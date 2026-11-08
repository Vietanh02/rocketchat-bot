"""Documents the assistant looks things up in.

Two kinds, told apart by scope:

- the knowledge base ("base"), uploaded by admins on the portal and read in
  every conversation;
- documents someone sends in a conversation ("conv:<session key>"), read
  only there, and forgotten when the conversation starts afresh (`new`).

A question that asks to look something up is searched in the
conversation's own documents first; when it has none, or none of their
passages match, in the knowledge base (`lookup`).

A document is parsed (in a thread; it is CPU work), cut into passages and
stored at once, so its words can be searched straight away. Vectors follow
in the background, as for memory notes, and are redone when the embedding
model changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import EmbeddingSettings
from app.knowledge.chunking import chunk_pages
from app.knowledge.embeddings import EmbeddingError, model_tag
from app.knowledge.parsers import ParseError, parse
from app.knowledge.search import fts_query, rrf
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import Point, VectorStoreError
from app.models import KbChunk, KbDocument

log = logging.getLogger(__name__)

COLLECTION = "kb_chunks"
BASE_SCOPE = "base"
# Documents one conversation may hold.
MAX_CONVERSATION_DOCUMENTS = 20
# Results taken from each search before merging and reranking.
CANDIDATES = 30
INDEX_BATCH = 32
INDEX_RETRY_SECONDS = 60.0
# Documents parsed at the same time; parsing a big PDF takes a core.
PARSE_SLOTS = 2


def conversation_scope(session_key: str) -> str:
    return f"conv:{session_key}"


class DocumentError(ValueError):
    """A document was refused; the message says why, for the person who sent it."""


@dataclass(slots=True)
class DocumentInfo:
    id: int
    scope: str
    title: str
    filename: str
    content_type: str
    size_bytes: int
    pages: int | None
    chunks: int
    chars: int
    truncated: bool
    source: str
    uploaded_by: str | None
    created_at: datetime | None
    # Passages still without a vector of the current model (set by `documents`).
    pending: int = 0

    @classmethod
    def from_row(cls, row: KbDocument) -> DocumentInfo:
        return cls(
            row.id,
            row.scope,
            row.title,
            row.filename,
            row.content_type,
            row.size_bytes,
            row.pages,
            row.chunks,
            row.chars,
            row.truncated,
            row.source,
            row.uploaded_by,
            row.created_at,
        )


@dataclass(slots=True)
class Passage:
    id: int
    document_id: int
    scope: str
    title: str
    text: str
    heading: str = ""
    page: int | None = None
    score: float | None = None

    @property
    def label(self) -> str:
        """How the passage is cited: title, and the page or section."""
        where = f", page {self.page}" if self.page else (f" — {self.heading}" if self.heading else "")
        return f"{self.title}{where}"


@dataclass(slots=True)
class Lookup:
    passages: list[Passage]
    # "conversation", "base", or None when nothing was found.
    source: str | None
    # The scopes searched that have documents at all.
    available: set[str] = field(default_factory=set)


class KnowledgeBase:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], knowledge: KnowledgeService
    ) -> None:
        self._session_factory = session_factory
        self.knowledge = knowledge
        self._parse_slots = asyncio.Semaphore(PARSE_SLOTS)
        self._indexed_tag: str | None = None
        self._index_task: asyncio.Task | None = None
        self._index_retry_at = 0.0

    # ----------------------------------------------------------------- #
    # Documents
    # ----------------------------------------------------------------- #

    async def add(
        self,
        scope: str,
        filename: str,
        data: bytes,
        *,
        content_type: str | None = None,
        embedding: EmbeddingSettings,
        source: str = "portal",
        uploaded_by: str | None = None,
    ) -> tuple[DocumentInfo, bool]:
        """Read and store a document; (the document, False) if the scope already has it."""
        digest = hashlib.sha256(data).hexdigest()
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(KbDocument).where(KbDocument.scope == scope, KbDocument.content_hash == digest)
            )
            if existing is not None:
                return DocumentInfo.from_row(existing), False
            if scope != BASE_SCOPE:
                count = await db.scalar(
                    select(func.count()).select_from(KbDocument).where(KbDocument.scope == scope)
                )
                if (count or 0) >= MAX_CONVERSATION_DOCUMENTS:
                    raise DocumentError(
                        f"this conversation already has {MAX_CONVERSATION_DOCUMENTS} documents; "
                        "send `new` to start afresh"
                    )

        async with self._parse_slots:
            try:
                parsed = await asyncio.to_thread(parse, filename, data, content_type)
            except ParseError as exc:
                raise DocumentError(str(exc)) from exc
            chunks = await asyncio.to_thread(chunk_pages, parsed.pages)
        if not chunks:
            raise DocumentError("no text could be read from it")

        page_numbers = [page.number for page in parsed.pages if page.number]
        async with self._session_factory() as db:
            row = KbDocument(
                scope=scope,
                title=parsed.title[:300],
                filename=filename[:300],
                content_type=(content_type or "")[:128],
                size_bytes=len(data),
                content_hash=digest,
                status="ready",
                pages=max(page_numbers) if page_numbers else None,
                chunks=len(chunks),
                chars=parsed.chars,
                truncated=parsed.truncated,
                source=source,
                uploaded_by=uploaded_by,
            )
            db.add(row)
            await db.flush()
            db.add_all(
                KbChunk(
                    document_id=row.id,
                    scope=scope,
                    ord=index,
                    text=chunk.text,
                    heading=chunk.heading[:300],
                    page=chunk.page,
                )
                for index, chunk in enumerate(chunks)
            )
            await db.commit()
            await db.refresh(row)
            info = DocumentInfo.from_row(row)
        log.info(
            "Added %s to %s: %d passage(s), %d characters", filename, scope, len(chunks), parsed.chars
        )
        # Words are searchable now; vectors follow.
        self._indexed_tag = None
        self._index_retry_at = 0.0
        self.ensure_indexed(embedding)
        return info, True

    async def delete(self, document_id: int, scopes: Sequence[str] | None = None) -> DocumentInfo | None:
        async with self._session_factory() as db:
            row = await db.get(KbDocument, document_id)
            if row is None or (scopes is not None and row.scope not in scopes):
                return None
            gone = DocumentInfo.from_row(row)
            ids = list(
                (await db.scalars(select(KbChunk.id).where(KbChunk.document_id == document_id))).all()
            )
            # Explicitly, so the word index triggers run for every passage.
            await db.execute(delete(KbChunk).where(KbChunk.document_id == document_id))
            await db.delete(row)
            await db.commit()
        await self._drop_vectors(ids)
        return gone

    async def forget_scope(self, scope: str) -> int:
        """Delete every document of a scope; how many there were."""
        async with self._session_factory() as db:
            documents = list(
                (await db.scalars(select(KbDocument.id).where(KbDocument.scope == scope))).all()
            )
            if not documents:
                return 0
            ids = list((await db.scalars(select(KbChunk.id).where(KbChunk.scope == scope))).all())
            await db.execute(delete(KbChunk).where(KbChunk.scope == scope))
            await db.execute(delete(KbDocument).where(KbDocument.scope == scope))
            await db.commit()
        await self._drop_vectors(ids)
        return len(documents)

    async def _drop_vectors(self, ids: list[int]) -> None:
        if ids and self.knowledge.store is not None:
            try:
                await self.knowledge.store.delete(COLLECTION, ids)
            except VectorStoreError as exc:
                # Harmless: searches only return passages still in the table.
                log.warning("Could not delete %d passage vector(s): %s", len(ids), exc)

    async def documents(
        self, *, scope: str | None = None, source: str | None = None, embedding: EmbeddingSettings | None = None
    ) -> list[DocumentInfo]:
        conditions = []
        if scope:
            conditions.append(KbDocument.scope == scope)
        if source:
            conditions.append(KbDocument.source == source)
        async with self._session_factory() as db:
            rows = (
                await db.scalars(select(KbDocument).where(*conditions).order_by(KbDocument.id.desc()))
            ).all()
            infos = [DocumentInfo.from_row(row) for row in rows]
            tag = model_tag(embedding.model_id) if embedding is not None else ""
            if tag and infos:
                pending = dict(
                    (
                        await db.execute(
                            select(KbChunk.document_id, func.count())
                            .where(
                                KbChunk.document_id.in_([info.id for info in infos]),
                                KbChunk.embedded_model != tag,
                            )
                            .group_by(KbChunk.document_id)
                        )
                    ).all()
                )
                for info in infos:
                    info.pending = pending.get(info.id, 0)
        return infos

    async def scopes_with_documents(self, scopes: Sequence[str]) -> set[str]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(KbDocument.scope).where(KbDocument.scope.in_(list(scopes))).distinct()
            )
            return set(rows.all())

    async def titles(self, scope: str) -> list[str]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(KbDocument.title).where(KbDocument.scope == scope).order_by(KbDocument.id)
            )
            return list(rows.all())

    # ----------------------------------------------------------------- #
    # Searching
    # ----------------------------------------------------------------- #

    async def lookup(
        self,
        question: str,
        conversation: str,
        *,
        embedding: EmbeddingSettings,
        limit: int,
        min_score: float,
        about_document: bool = False,
    ) -> Lookup:
        """Passages for a question: the conversation's own documents first, then the base.

        With `about_document` ("summarize this file"), a conversation whose
        documents match none of the question's words gets the beginning of
        the latest one instead of the knowledge base.
        """
        present = await self.scopes_with_documents([conversation, BASE_SCOPE])
        for scope, source in ((conversation, "conversation"), (BASE_SCOPE, "base")):
            if scope not in present:
                continue
            passages = await self.search(
                question, [scope], embedding=embedding, limit=limit, min_score=min_score
            )
            if passages:
                return Lookup(passages, source, present)
            if scope == conversation and about_document:
                return Lookup(await self.beginning(conversation, limit), source, present)
        return Lookup([], None, present)

    async def beginning(self, scope: str, limit: int) -> list[Passage]:
        """The first passages of the latest document of a scope."""
        async with self._session_factory() as db:
            latest = await db.scalar(
                select(KbDocument).where(KbDocument.scope == scope).order_by(KbDocument.id.desc())
            )
            if latest is None:
                return []
            chunks = (
                await db.scalars(
                    select(KbChunk)
                    .where(KbChunk.document_id == latest.id)
                    .order_by(KbChunk.ord)
                    .limit(limit)
                )
            ).all()
        return [
            Passage(c.id, c.document_id, c.scope, latest.title, c.text, c.heading, c.page)
            for c in chunks
        ]

    async def search(
        self,
        query: str,
        scopes: Sequence[str],
        *,
        embedding: EmbeddingSettings,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[Passage]:
        dense, words = await asyncio.gather(
            self._dense(query, scopes, embedding, min_score), self._words(query, scopes)
        )
        scores = dict(rrf([dense, words]))
        merged = list(scores)[:CANDIDATES]
        if not merged:
            return []
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(KbChunk, KbDocument.title)
                    .join(KbDocument, KbDocument.id == KbChunk.document_id)
                    .where(KbChunk.id.in_(merged), KbChunk.scope.in_(list(scopes)))
                )
            ).all()
        by_id = {
            chunk.id: Passage(
                chunk.id, chunk.document_id, chunk.scope, title, chunk.text, chunk.heading, chunk.page
            )
            for chunk, title in rows
        }
        passages = [by_id[i] for i in merged if i in by_id]
        for passage in passages:
            passage.score = scores[passage.id]

        reranker = self.knowledge.reranker_for(embedding)
        if reranker is not None and len(passages) > 1:
            try:
                reranked = await reranker.rerank(query, [p.text for p in passages])
            except EmbeddingError as exc:
                log.warning("Reranking passages failed, keeping the merged order: %s", exc)
            else:
                for passage, score in zip(passages, reranked, strict=True):
                    passage.score = score
                passages.sort(key=lambda p: p.score or 0.0, reverse=True)
        return passages[:limit]

    async def _dense(
        self,
        query: str,
        scopes: Sequence[str],
        embedding: EmbeddingSettings,
        min_score: float | None,
    ) -> list[int]:
        vectors = self.knowledge.vectors_for(embedding)
        if vectors is None:
            return []
        embedder, store = vectors
        try:
            vector = await embedder.embed_query(query)
            hits = await store.search(
                COLLECTION,
                vector,
                limit=CANDIDATES,
                where={"scope": list(scopes), "model": [embedder.tag]},
                min_score=min_score,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            log.warning("Searching documents by meaning failed, using words only: %s", exc)
            return []
        return [hit.id for hit in hits]

    async def _words(self, query: str, scopes: Sequence[str]) -> list[int]:
        match = fts_query(query, skip_stopwords=True)
        if match is None:
            return []
        statement = text(
            "SELECT c.id FROM kb_chunk_fts JOIN kb_chunk AS c ON c.id = kb_chunk_fts.rowid "
            "WHERE kb_chunk_fts MATCH :match AND c.scope IN :scopes "
            "ORDER BY bm25(kb_chunk_fts) LIMIT :limit"
        ).bindparams(bindparam("scopes", expanding=True))
        async with self._session_factory() as db:
            rows = await db.execute(
                statement, {"match": match, "scopes": list(scopes), "limit": CANDIDATES}
            )
            return [row[0] for row in rows]

    # ----------------------------------------------------------------- #
    # Vectors
    # ----------------------------------------------------------------- #

    def ensure_indexed(self, embedding: EmbeddingSettings) -> None:
        """Embed, in the background, the passages the current model has no vector for."""
        tag = model_tag(embedding.model_id)
        if not tag or self.knowledge.store is None or self._indexed_tag == tag:
            return
        if self._index_task is not None and not self._index_task.done():
            return
        if time.monotonic() < self._index_retry_at:
            return
        self._index_task = asyncio.create_task(self._index(embedding), name="kb-index")

    async def _index(self, embedding: EmbeddingSettings) -> None:
        try:
            done = await self.index_pending(embedding)
            if done:
                log.info("Embedded %d document passage(s)", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retried later
            log.warning("Could not embed the pending passages", exc_info=True)
        if self._indexed_tag != model_tag(embedding.model_id):
            self._index_retry_at = time.monotonic() + INDEX_RETRY_SECONDS

    async def index_pending(self, embedding: EmbeddingSettings) -> int:
        vectors = self.knowledge.vectors_for(embedding)
        if vectors is None:
            return 0
        embedder, store = vectors
        tag = embedder.tag
        done = 0
        while True:
            async with self._session_factory() as db:
                rows = (
                    await db.scalars(
                        select(KbChunk)
                        .where(KbChunk.embedded_model != tag)
                        .order_by(KbChunk.id)
                        .limit(INDEX_BATCH)
                    )
                ).all()
            if not rows:
                break
            try:
                made = await embedder.embed_documents(
                    [f"{row.heading}\n{row.text}" if row.heading else row.text for row in rows]
                )
                if await store.ensure_collection(COLLECTION, len(made[0])):
                    # A new collection: every other vector went with the old one.
                    async with self._session_factory() as db:
                        await db.execute(update(KbChunk).values(embedded_model=""))
                        await db.commit()
                await store.upsert(
                    COLLECTION,
                    [
                        Point(
                            row.id,
                            vector,
                            {"scope": row.scope, "model": tag, "document_id": row.document_id},
                        )
                        for row, vector in zip(rows, made, strict=True)
                    ],
                )
            except (EmbeddingError, VectorStoreError) as exc:
                log.warning("Embedding passages failed: %s", exc)
                return done
            async with self._session_factory() as db:
                await db.execute(
                    update(KbChunk)
                    .where(KbChunk.id.in_([row.id for row in rows]))
                    .values(embedded_model=tag)
                )
                await db.commit()
            done += len(rows)
        self._indexed_tag = tag
        return done

    async def pending_count(self, embedding: EmbeddingSettings) -> int:
        tag = model_tag(embedding.model_id)
        async with self._session_factory() as db:
            return await db.scalar(
                select(func.count()).select_from(KbChunk).where(KbChunk.embedded_model != tag)
            ) or 0

    async def close(self) -> None:
        if self._index_task is not None:
            self._index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._index_task
