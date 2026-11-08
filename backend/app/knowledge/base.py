"""Documents the assistant looks things up in.

Two kinds, told apart by scope:

- the knowledge base ("base"), uploaded by admins on the portal and read in
  every conversation;
- documents someone sends in a conversation ("conv:<session key>"), read
  only there, and forgotten when the conversation starts afresh (`new`).

**Adding a document** takes three steps; the first is done before `add`
returns, so the document can be searched by its words at once.

1. Its text is read as written (`parsers`), kept whole in `kb_source` with
   the uploaded file, and cut by its headings into passages that are exact
   slices of it (`chunking`).
2. In the background, the language model indexes it (`indexer`): it groups
   the blocks into sections and describes each (title, summary, keywords
   with synonyms, likely questions). The passages are cut again along those
   sections, still as slices of the text, and carry the descriptions into
   the search index.
3. Each passage gets vectors: one of its section title and text, and one
   per likely question, so a question finds the passage whether it shares
   its words, its meaning, or the way someone would ask it.

**Looking a question up** (`lookup`) searches the conversation's own
documents first and the knowledge base when they have nothing. Word search
(FTS5, weighting titles, keywords and questions above body text) and vector
search are merged by RRF; the language model then keeps the passages that
actually answer, best first; and each is shown with its neighbours in the
same section, again as one exact slice of the document.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePath
from typing import TYPE_CHECKING

from sqlalchemy import bindparam, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import EmbeddingSettings
from app.config import get_settings
from app.knowledge.chunking import Block, Section, Span, chunk_pages, pack
from app.knowledge.embeddings import EmbeddingError, model_tag
from app.knowledge.indexer import index_sections, judge
from app.knowledge.parsers import ParseError, extension_for, parse
from app.knowledge.search import fts_query, rrf
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import Point, VectorStoreError
from app.models import KbDocument, KbPassage, KbSource

if TYPE_CHECKING:
    from app.bot.config_store import ConfigSnapshot
    from app.bot.llm import LLMChat

log = logging.getLogger(__name__)

COLLECTION = "kb_passages"
BASE_SCOPE = "base"
# Documents one conversation may hold.
MAX_CONVERSATION_DOCUMENTS = 20
# Results taken from each search before merging.
CANDIDATES = 30
# Passages the language model is asked to choose from.
JUDGE_CANDIDATES = 12
# A passage is shown with its neighbours up to this long in all.
EXPAND_CHARS = 2400
# Point ids: passage id * POINTS_PER_PASSAGE + k; k = 0 for the passage, 1… its questions.
POINTS_PER_PASSAGE = 8
QUESTION_VECTORS = 5
INDEX_BATCH = 32
INDEX_RETRY_SECONDS = 60.0
PARSE_SLOTS = 2
# Word search weights: text, section title, keywords, questions, summary.
BM25_WEIGHTS = "1.0, 4.0, 2.5, 2.0, 1.0"

ModelAccess = Callable[[], Awaitable["tuple[ConfigSnapshot, LLMChat | None]"]]


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
    stage: str = "queued"
    stage_error: str | None = None
    sections: int = 0
    # Passages still without a vector of the current model (set by `documents`).
    pending: int = 0

    @classmethod
    def from_row(cls, row: KbDocument, source: KbSource | None = None) -> DocumentInfo:
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
            stage=source.stage if source is not None else "basic",
            stage_error=source.stage_error if source is not None else None,
        )


@dataclass(slots=True)
class Passage:
    id: int
    document_id: int
    scope: str
    title: str
    text: str
    section: str = ""
    page: int | None = None
    score: float | None = None
    context: str = ""
    ord: int = 0
    section_ord: int = 0
    start: int = 0
    end: int = 0

    @property
    def label(self) -> str:
        """How the passage is cited: title, and the page or section."""
        where = f", page {self.page}" if self.page else (f" — {self.section}" if self.section else "")
        return f"{self.title}{where}"

    @property
    def shown(self) -> str:
        """The text as the model reads it: a table's header first, marked as such."""
        if self.context:
            return f"(table header: {self.context})\n{self.text}"
        return self.text


@dataclass(slots=True)
class Lookup:
    passages: list[Passage]
    # "conversation", "base", or None when nothing was found.
    source: str | None
    # The scopes searched that have documents at all.
    available: set[str] = field(default_factory=set)
    # Whether the language model chose the passages.
    judged: bool = False


def _blocks_to_json(blocks: Sequence[Block]) -> str:
    return json.dumps([[b.start, b.end, b.kind, b.page, b.level, b.header] for b in blocks])


def _blocks_from_json(raw: str) -> list[Block]:
    return [Block(s, e, k, p, lv, h) for s, e, k, p, lv, h in json.loads(raw or "[]")]


def _safe_filename(name: str) -> str:
    stem = re.sub(r"[^\w.-]+", "_", PurePath(name).name, flags=re.UNICODE).strip("._")
    return (stem or "document")[:120]


def point_ids(passage_ids: Sequence[int]) -> list[int]:
    return [pid * POINTS_PER_PASSAGE + k for pid in passage_ids for k in range(POINTS_PER_PASSAGE)]


class KnowledgeBase:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], knowledge: KnowledgeService
    ) -> None:
        self._session_factory = session_factory
        self.knowledge = knowledge
        # Set by the responder: the current settings and the model to index with.
        self.model_access: ModelAccess | None = None
        self._parse_slots = asyncio.Semaphore(PARSE_SLOTS)
        self._indexed_tag: str | None = None
        self._index_task: asyncio.Task | None = None
        self._index_retry_at = 0.0
        self._pipeline_task: asyncio.Task | None = None
        self._pipeline_again = False

    @property
    def files_dir(self) -> Path:
        return Path(get_settings().kb_files_dir)

    # ----------------------------------------------------------------- #
    # Adding and removing documents
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
        """Read and store a document; (the document, False) if the scope already has it.

        The document is searchable by its words when this returns; the
        model's indexing and the vectors follow in the background.
        """
        digest = hashlib.sha256(data).hexdigest()
        async with self._session_factory() as db:
            existing = await db.scalar(
                select(KbDocument).where(KbDocument.scope == scope, KbDocument.content_hash == digest)
            )
            if existing is not None:
                return DocumentInfo.from_row(existing, await db.get(KbSource, existing.id)), False
            if scope != BASE_SCOPE:
                count = await db.scalar(
                    select(func.count()).select_from(KbDocument).where(KbDocument.scope == scope)
                )
                if (count or 0) >= MAX_CONVERSATION_DOCUMENTS:
                    raise DocumentError(
                        f"this conversation already has {MAX_CONVERSATION_DOCUMENTS} documents; "
                        "send `new` to start afresh"
                    )

        tabular = extension_for(filename, content_type) in (".csv", ".tsv")
        async with self._parse_slots:
            try:
                parsed = await asyncio.to_thread(parse, filename, data, content_type)
            except ParseError as exc:
                raise DocumentError(str(exc)) from exc
            body, blocks, sections, spans = await asyncio.to_thread(
                chunk_pages, parsed.pages, tabular=tabular
            )
        if not spans:
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
                chunks=len(spans),
                chars=len(body),
                truncated=parsed.truncated,
                source=source,
                uploaded_by=uploaded_by,
            )
            db.add(row)
            await db.flush()
            path = self.files_dir / str(row.id) / _safe_filename(filename)
            db.add(
                KbSource(
                    document_id=row.id,
                    text=body,
                    blocks_json=_blocks_to_json(blocks),
                    file_path=str(path),
                    stage="queued",
                )
            )
            db.add_all(self._passage_rows(row.id, scope, body, sections, spans))
            await db.commit()
            await db.refresh(row)
            source_row = await db.get(KbSource, row.id)
            info = DocumentInfo.from_row(row, source_row)
        try:
            await asyncio.to_thread(_write_file, path, data)
        except OSError as exc:
            log.warning("Could not keep the uploaded file %s: %s", path, exc)
        info.sections = len(sections)
        log.info(
            "Added %s to %s: %d characters, %d passages", filename, scope, len(body), len(spans)
        )
        self.ensure_pipeline()
        self._indexed_tag = None
        self._index_retry_at = 0.0
        self.ensure_indexed(embedding)
        return info, True

    @staticmethod
    def _passage_rows(
        document_id: int, scope: str, body: str, sections: Sequence[Section], spans: Sequence[Span]
    ) -> list[KbPassage]:
        rows = []
        for ord_, span in enumerate(spans):
            section = sections[span.section]
            rows.append(
                KbPassage(
                    document_id=document_id,
                    scope=scope,
                    ord=ord_,
                    section_ord=span.section,
                    start=span.start,
                    end=span.end,
                    page=span.page,
                    text=body[span.start : span.end],
                    context=span.context,
                    section=section.title[:300],
                    summary=section.summary,
                    keywords=" ".join(section.keywords),
                    questions="\n".join(section.questions),
                )
            )
        return rows

    async def delete(self, document_id: int, scopes: Sequence[str] | None = None) -> DocumentInfo | None:
        async with self._session_factory() as db:
            row = await db.get(KbDocument, document_id)
            if row is None or (scopes is not None and row.scope not in scopes):
                return None
            gone = DocumentInfo.from_row(row)
            ids = list(
                (await db.scalars(select(KbPassage.id).where(KbPassage.document_id == document_id))).all()
            )
            # Explicitly, so the word index triggers run for every passage.
            await db.execute(delete(KbPassage).where(KbPassage.document_id == document_id))
            await db.execute(delete(KbSource).where(KbSource.document_id == document_id))
            await db.delete(row)
            await db.commit()
        await self._drop_vectors(ids)
        await self._drop_files([document_id])
        return gone

    async def forget_scope(self, scope: str) -> int:
        """Delete every document of a scope; how many there were."""
        async with self._session_factory() as db:
            documents = list(
                (await db.scalars(select(KbDocument.id).where(KbDocument.scope == scope))).all()
            )
            if not documents:
                return 0
            ids = list((await db.scalars(select(KbPassage.id).where(KbPassage.scope == scope))).all())
            await db.execute(delete(KbPassage).where(KbPassage.scope == scope))
            await db.execute(delete(KbSource).where(KbSource.document_id.in_(documents)))
            await db.execute(delete(KbDocument).where(KbDocument.scope == scope))
            await db.commit()
        await self._drop_vectors(ids)
        await self._drop_files(documents)
        return len(documents)

    async def _drop_vectors(self, passage_ids: list[int]) -> None:
        if passage_ids and self.knowledge.store is not None:
            try:
                await self.knowledge.store.delete(COLLECTION, point_ids(passage_ids))
            except VectorStoreError as exc:
                # Harmless: searches only return passages still in the table.
                log.warning("Could not delete %d passages' vectors: %s", len(passage_ids), exc)

    async def _drop_files(self, document_ids: list[int]) -> None:
        for document_id in document_ids:
            folder = self.files_dir / str(document_id)
            await asyncio.to_thread(shutil.rmtree, folder, True)

    async def reprocess(self, document_id: int) -> bool:
        """Have the language model index a document again."""
        async with self._session_factory() as db:
            source = await db.get(KbSource, document_id)
            if source is None:
                return False
            source.stage, source.stage_error = "queued", None
            await db.commit()
        self.ensure_pipeline()
        return True

    # ----------------------------------------------------------------- #
    # Listing
    # ----------------------------------------------------------------- #

    async def documents(
        self,
        *,
        scope: str | None = None,
        source: str | None = None,
        embedding: EmbeddingSettings | None = None,
    ) -> list[DocumentInfo]:
        conditions = []
        if scope:
            conditions.append(KbDocument.scope == scope)
        if source:
            conditions.append(KbDocument.source == source)
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(KbDocument, KbSource)
                    .outerjoin(KbSource, KbSource.document_id == KbDocument.id)
                    .where(*conditions)
                    .order_by(KbDocument.id.desc())
                )
            ).all()
            infos = [DocumentInfo.from_row(row, src) for row, src in rows]
            if not infos:
                return []
            ids = [info.id for info in infos]
            sections = dict(
                (
                    await db.execute(
                        select(KbPassage.document_id, func.count(func.distinct(KbPassage.section_ord)))
                        .where(KbPassage.document_id.in_(ids))
                        .group_by(KbPassage.document_id)
                    )
                ).all()
            )
            tag = model_tag(embedding.model_id) if embedding is not None else ""
            pending: dict[int, int] = {}
            if tag:
                pending = dict(
                    (
                        await db.execute(
                            select(KbPassage.document_id, func.count())
                            .where(KbPassage.document_id.in_(ids), KbPassage.embedded_model != tag)
                            .group_by(KbPassage.document_id)
                        )
                    ).all()
                )
        for info in infos:
            info.sections = sections.get(info.id, 0)
            info.pending = pending.get(info.id, 0)
        return infos

    async def scopes_with_documents(self, scopes: Sequence[str]) -> set[str]:
        async with self._session_factory() as db:
            rows = await db.scalars(
                select(KbDocument.scope).where(KbDocument.scope.in_(list(scopes))).distinct()
            )
            return set(rows.all())

    async def stage_counts(self) -> dict[str, int]:
        async with self._session_factory() as db:
            rows = await db.execute(select(KbSource.stage, func.count()).group_by(KbSource.stage))
            return {stage: count for stage, count in rows}

    # ----------------------------------------------------------------- #
    # Indexing by the language model, in the background
    # ----------------------------------------------------------------- #

    def ensure_pipeline(self) -> None:
        """Index, in the background, every document waiting for the language model."""
        if self._pipeline_task is not None and not self._pipeline_task.done():
            self._pipeline_again = True
            return
        self._pipeline_task = asyncio.create_task(self._run_pipeline(), name="kb-pipeline")

    async def _run_pipeline(self) -> None:
        try:
            while True:
                self._pipeline_again = False
                async with self._session_factory() as db:
                    waiting = list(
                        (
                            await db.scalars(
                                select(KbSource.document_id)
                                .where(KbSource.stage.in_(["queued", "indexing"]))
                                .order_by(KbSource.document_id)
                            )
                        ).all()
                    )
                for document_id in waiting:
                    await self._index_document(document_id)
                if not self._pipeline_again:
                    break
            if self.model_access is not None:
                snapshot, _ = await self.model_access()
                self._indexed_tag = None
                self._index_retry_at = 0.0
                self.ensure_indexed(snapshot.embedding)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next upload or turn starts it again
            log.warning("The document pipeline stopped", exc_info=True)

    async def _set_stage(self, document_id: int, stage: str, error: str | None = None, **fields) -> None:
        async with self._session_factory() as db:
            await db.execute(
                update(KbSource)
                .where(KbSource.document_id == document_id)
                .values(stage=stage, stage_error=error, **fields)
            )
            await db.commit()

    async def _index_document(self, document_id: int) -> None:
        if self.model_access is None:
            await self._set_stage(document_id, "basic")
            return
        snapshot, chat = await self.model_access()
        if not snapshot.knowledge.llm_index or chat is None:
            await self._set_stage(document_id, "basic")
            return
        async with self._session_factory() as db:
            row = await db.get(KbDocument, document_id)
            source = await db.get(KbSource, document_id)
            if row is None or source is None:
                return
            title, scope, body = row.title, row.scope, source.text
            blocks = _blocks_from_json(source.blocks_json)
        await self._set_stage(document_id, "indexing")
        model = snapshot.knowledge.index_model or snapshot.agent.fast_model or None
        started = time.monotonic()
        try:
            sections, fallbacks = await index_sections(chat, body, blocks, title=title, model=model)
        except asyncio.CancelledError:
            # Picked up again by the next pipeline run.
            with contextlib.suppress(Exception):
                await self._set_stage(document_id, "queued")
            raise
        except Exception as exc:  # noqa: BLE001 - the heading cut stays in use
            log.warning("Could not index %r with the language model: %s", title, exc)
            await self._set_stage(document_id, "failed", str(exc)[:500])
            return
        spans = pack(body, blocks, sections)
        old_ids: list[int] = []
        async with self._session_factory() as db:
            if await db.get(KbDocument, document_id) is None:
                return  # deleted meanwhile
            old_ids = list(
                (await db.scalars(select(KbPassage.id).where(KbPassage.document_id == document_id))).all()
            )
            await db.execute(delete(KbPassage).where(KbPassage.document_id == document_id))
            db.add_all(self._passage_rows(document_id, scope, body, sections, spans))
            await db.execute(
                update(KbDocument).where(KbDocument.id == document_id).values(chunks=len(spans))
            )
            await db.execute(
                update(KbSource)
                .where(KbSource.document_id == document_id)
                .values(
                    stage="indexed",
                    stage_error=None,
                    indexed_by=model or chat.model,
                    fallback_windows=fallbacks,
                )
            )
            await db.commit()
        await self._drop_vectors(old_ids)
        log.info(
            "Indexed %r with the language model in %.1fs: %d sections, %d passages",
            title,
            time.monotonic() - started,
            len(sections),
            len(spans),
        )

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
        judge_with: tuple[LLMChat, str | None] | None = None,
    ) -> Lookup:
        """Passages for a question: the conversation's own documents first, then the base.

        With `judge_with` (a chat and model), the language model keeps the
        passages that answer the question; when it keeps none of the
        conversation's, the knowledge base is tried. With `about_document`
        ("summarize this file"), a conversation whose documents match
        nothing gets the beginning of the latest one instead.
        """
        present = await self.scopes_with_documents([conversation, BASE_SCOPE])
        for scope, source in ((conversation, "conversation"), (BASE_SCOPE, "base")):
            if scope not in present:
                continue
            found = await self.search(
                question,
                [scope],
                embedding=embedding,
                limit=max(limit, JUDGE_CANDIDATES) if judge_with else limit,
                min_score=min_score,
            )
            judged = False
            if found and judge_with is not None:
                chat, model = judge_with
                picked = await judge(chat, question, [(p.label, p.shown) for p in found], model=model)
                if picked is not None:
                    found, judged = [found[i] for i in picked], True
            if found:
                return Lookup(await self.expand(found[:limit]), source, present, judged)
            if scope == conversation and about_document:
                return Lookup(await self.beginning(conversation, limit), source, present)
        return Lookup([], None, present)

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
        passages = await self._load(merged, scopes)
        for passage in passages:
            passage.score = scores[passage.id]

        reranker = self.knowledge.reranker_for(embedding)
        if reranker is not None and len(passages) > 1:
            try:
                reranked = await reranker.rerank(query, [p.shown for p in passages])
            except EmbeddingError as exc:
                log.warning("Reranking passages failed, keeping the merged order: %s", exc)
            else:
                for passage, score in zip(passages, reranked, strict=True):
                    passage.score = score
                passages.sort(key=lambda p: p.score or 0.0, reverse=True)
        return passages[:limit]

    async def _load(self, ids: Sequence[int], scopes: Sequence[str]) -> list[Passage]:
        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    select(KbPassage, KbDocument.title)
                    .join(KbDocument, KbDocument.id == KbPassage.document_id)
                    .where(KbPassage.id.in_(list(ids)), KbPassage.scope.in_(list(scopes)))
                )
            ).all()
        by_id = {row.id: _passage(row, title) for row, title in rows}
        return [by_id[i] for i in ids if i in by_id]

    async def expand(self, passages: Sequence[Passage]) -> list[Passage]:
        """Each passage with its neighbours in the same section, as one exact slice."""
        if not passages:
            return []
        async with self._session_factory() as db:
            out: list[Passage] = []
            covered: dict[int, list[tuple[int, int]]] = {}
            for passage in passages:
                if any(s <= passage.start < e for s, e in covered.get(passage.document_id, [])):
                    continue  # already shown inside an earlier one
                neighbours = (
                    await db.scalars(
                        select(KbPassage).where(
                            KbPassage.document_id == passage.document_id,
                            KbPassage.section_ord == passage.section_ord,
                            KbPassage.ord.in_([passage.ord - 1, passage.ord + 1]),
                        )
                    )
                ).all()
                start, end = passage.start, passage.end
                for neighbour in sorted(neighbours, key=lambda n: n.ord, reverse=True):
                    # The one after first: an answer usually follows what matched.
                    new_start, new_end = min(start, neighbour.start), max(end, neighbour.end)
                    if new_end - new_start <= EXPAND_CHARS:
                        start, end = new_start, new_end
                if (start, end) != (passage.start, passage.end):
                    slice_ = await db.scalar(
                        select(func.substr(KbSource.text, start + 1, end - start)).where(
                            KbSource.document_id == passage.document_id
                        )
                    )
                    if slice_:
                        if start < passage.start:
                            passage.context = ""  # the table's header is in the text now
                        passage.text, passage.start, passage.end = slice_, start, end
                covered.setdefault(passage.document_id, []).append((start, end))
                out.append(passage)
        return out

    async def beginning(self, scope: str, limit: int) -> list[Passage]:
        """The first passages of the latest document of a scope."""
        async with self._session_factory() as db:
            latest = await db.scalar(
                select(KbDocument).where(KbDocument.scope == scope).order_by(KbDocument.id.desc())
            )
            if latest is None:
                return []
            rows = (
                await db.scalars(
                    select(KbPassage)
                    .where(KbPassage.document_id == latest.id)
                    .order_by(KbPassage.ord)
                    .limit(limit)
                )
            ).all()
        return [_passage(row, latest.title) for row in rows]

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
                limit=CANDIDATES * 3,
                where={"scope": list(scopes), "model": [embedder.tag]},
                min_score=min_score,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            log.warning("Searching documents by meaning failed, using words only: %s", exc)
            return []
        # A passage may be found through itself and through its questions; once is enough.
        ranked: list[int] = []
        for hit in hits:
            passage_id = int(hit.payload.get("passage_id", hit.id // POINTS_PER_PASSAGE))
            if passage_id not in ranked:
                ranked.append(passage_id)
        return ranked[:CANDIDATES]

    async def _words(self, query: str, scopes: Sequence[str]) -> list[int]:
        match = fts_query(query, skip_stopwords=True)
        if match is None:
            return []
        statement = text(
            "SELECT p.id FROM kb_passage_fts JOIN kb_passage AS p ON p.id = kb_passage_fts.rowid "
            "WHERE kb_passage_fts MATCH :match AND p.scope IN :scopes "
            f"ORDER BY bm25(kb_passage_fts, {BM25_WEIGHTS}) LIMIT :limit"
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
        """Embed, in the background, the passages the current model has no vectors for."""
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
                    await db.execute(
                        select(KbPassage, KbDocument.title)
                        .join(KbDocument, KbDocument.id == KbPassage.document_id)
                        .where(KbPassage.embedded_model != tag)
                        .order_by(KbPassage.id)
                        .limit(INDEX_BATCH)
                    )
                ).all()
            if not rows:
                break
            contents = []
            questions: list[tuple[int, int, str]] = []
            for passage, title in rows:
                heading = " — ".join(part for part in (title, passage.section) if part)
                body = f"{passage.context}\n{passage.text}" if passage.context else passage.text
                contents.append(f"{heading}\n{body}" if heading else body)
                asked = [q for q in passage.questions.split("\n") if q.strip()][:QUESTION_VECTORS]
                questions.extend((passage.id, k, q) for k, q in enumerate(asked, start=1))
            try:
                made = await embedder.embed_documents(contents)
                asked_vectors = (
                    await embedder.embed_queries([q for _, _, q in questions]) if questions else []
                )
                if await store.ensure_collection(COLLECTION, len(made[0])):
                    # A new collection: every other vector went with the old one.
                    async with self._session_factory() as db:
                        await db.execute(update(KbPassage).values(embedded_model=""))
                        await db.commit()
                scope_of = {passage.id: passage.scope for passage, _ in rows}
                points = [
                    Point(
                        passage.id * POINTS_PER_PASSAGE,
                        vector,
                        {"scope": passage.scope, "model": tag, "passage_id": passage.id, "kind": "text"},
                    )
                    for (passage, _), vector in zip(rows, made, strict=True)
                ]
                points += [
                    Point(
                        passage_id * POINTS_PER_PASSAGE + k,
                        vector,
                        {
                            "scope": scope_of[passage_id],
                            "model": tag,
                            "passage_id": passage_id,
                            "kind": "question",
                        },
                    )
                    for (passage_id, k, _), vector in zip(questions, asked_vectors, strict=True)
                ]
                await store.upsert(COLLECTION, points)
            except (EmbeddingError, VectorStoreError) as exc:
                log.warning("Embedding passages failed: %s", exc)
                return done
            async with self._session_factory() as db:
                await db.execute(
                    update(KbPassage)
                    .where(KbPassage.id.in_([passage.id for passage, _ in rows]))
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
                select(func.count()).select_from(KbPassage).where(KbPassage.embedded_model != tag)
            ) or 0

    async def close(self) -> None:
        for task in (self._index_task, self._pipeline_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task


def _passage(row: KbPassage, title: str) -> Passage:
    return Passage(
        row.id,
        row.document_id,
        row.scope,
        title,
        row.text,
        row.section,
        row.page,
        context=row.context,
        ord=row.ord,
        section_ord=row.section_ord,
        start=row.start,
        end=row.end,
    )


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
