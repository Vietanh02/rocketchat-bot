"""Notes the assistant keeps between conversations.

A note lives in the `agent_memory` table, which is the source of truth; its
words are indexed by FTS5 (see `app.models`) and, when an embedding model
and Qdrant are set up, its meaning by a vector in the `memory` collection.
Searching uses both (`app.knowledge.search`), or the words alone otherwise.

Who sees a note is its *scope*, and the scope always comes from where the
conversation happens (`MemoryScopes.for_conversation`), never from what
the model asks: a DM reads and writes that person's notes, a channel or
thread the channel's notes, and both read the global notes, which only the
portal writes. A channel therefore never sees what someone told the bot in
private.

Vectors are made when a note is saved. Notes without one (the embedding
server was down, or the model changed) are embedded again in the
background, and searches only compare vectors of the current model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import bindparam, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import EmbeddingSettings
from app.bot.types import MessageContext
from app.knowledge.embeddings import EmbeddingError, model_tag
from app.knowledge.search import fts_query, rrf
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import Point, VectorStoreError
from app.models import AgentMemory

log = logging.getLogger(__name__)

COLLECTION = "memory"
GLOBAL_SCOPE = "global"
MAX_NOTE_CHARS = 1000
# A scope holding this many notes takes no more until some are forgotten.
MAX_NOTES_PER_SCOPE = 500
# Results taken from each search before merging and reranking.
CANDIDATES = 20
# A new note this similar to an existing one of the same scope is not added.
DUPLICATE_SCORE = 0.95
# Notes embedded per request when catching up.
INDEX_BATCH = 64
# After catching up failed, it is not tried again for this long.
INDEX_RETRY_SECONDS = 60.0


class NoteError(Exception):
    """A note could not be saved; the message says why, for the model."""


@dataclass(slots=True, frozen=True)
class MemoryScopes:
    # Scopes whose notes the conversation may read.
    read: tuple[str, ...]
    # The one scope it writes to.
    write: str

    @classmethod
    def for_conversation(cls, message: MessageContext, kind: str) -> MemoryScopes:
        if kind == "dm":
            own = user_scope(message.sender_id)
        else:
            own = room_scope(message.room_id)
        return cls(read=(own, GLOBAL_SCOPE), write=own)


def user_scope(rc_user_id: str) -> str:
    return f"user:{rc_user_id}"


def room_scope(room_id: str) -> str:
    return f"room:{room_id}"


@dataclass(slots=True)
class Note:
    id: int
    scope: str
    text: str
    source: str = "tool"
    created_by: str | None = None
    created_at: datetime | None = None
    # How it was found: the merged rank score, or the reranker's.
    score: float | None = None

    @classmethod
    def from_row(cls, row: AgentMemory, score: float | None = None) -> Note:
        return cls(row.id, row.scope, row.text, row.source, row.created_by, row.created_at, score)


def clean_note(value: str) -> str:
    return " ".join(value.split())


class MemoryStore:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], knowledge: KnowledgeService
    ) -> None:
        self._session_factory = session_factory
        self.knowledge = knowledge
        # The model whose vectors are known to be complete; None to check again.
        self._indexed_tag: str | None = None
        self._index_task: asyncio.Task | None = None
        # time.monotonic() before which catching up is not tried again.
        self._index_retry_at = 0.0

    # ----------------------------------------------------------------- #
    # Writing
    # ----------------------------------------------------------------- #

    async def add(
        self,
        scope: str,
        note: str,
        *,
        embedding: EmbeddingSettings,
        source: str = "tool",
        created_by: str | None = None,
        session_id: int | None = None,
    ) -> tuple[Note, bool]:
        """Save a note; (the note, False) if the scope already says the same."""
        note = clean_note(note)
        if not note:
            raise NoteError("the note is empty")
        if len(note) > MAX_NOTE_CHARS:
            raise NoteError(f"a note may be at most {MAX_NOTE_CHARS} characters")

        async with self._session_factory() as db:
            # SQLite's lower() only folds ASCII, so compare in Python.
            same_length = (
                await db.scalars(
                    select(AgentMemory).where(
                        AgentMemory.scope == scope, func.length(AgentMemory.text) == len(note)
                    )
                )
            ).all()
            for row in same_length:
                if row.text.casefold() == note.casefold():
                    return Note.from_row(row), False
            count = await db.scalar(
                select(func.count()).select_from(AgentMemory).where(AgentMemory.scope == scope)
            )
        if (count or 0) >= MAX_NOTES_PER_SCOPE:
            raise NoteError(
                f"memory is full ({MAX_NOTES_PER_SCOPE} notes); forget some notes first"
            )

        vector, tag = await self._embed_note(note, embedding)
        if vector is not None:
            twin = await self._near_duplicate(scope, vector, tag)
            if twin is not None:
                return twin, False

        async with self._session_factory() as db:
            row = AgentMemory(
                scope=scope,
                text=note,
                source=source,
                created_by=created_by,
                session_id=session_id,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            saved = Note.from_row(row)
        if vector is not None:
            await self._store_vectors([(saved.id, scope, vector)], embedding, tag)
        return saved, True

    async def update_text(self, note_id: int, note: str, *, embedding: EmbeddingSettings) -> Note | None:
        note = clean_note(note)
        if not note or len(note) > MAX_NOTE_CHARS:
            raise NoteError(f"a note must have 1 to {MAX_NOTE_CHARS} characters")
        async with self._session_factory() as db:
            row = await db.get(AgentMemory, note_id)
            if row is None:
                return None
            row.text, row.embedded_model = note, ""
            await db.commit()
            await db.refresh(row)
            saved = Note.from_row(row)
        vector, tag = await self._embed_note(note, embedding)
        if vector is not None:
            await self._store_vectors([(saved.id, saved.scope, vector)], embedding, tag)
        return saved

    async def delete(self, note_id: int, scopes: Sequence[str] | None = None) -> Note | None:
        """Forget a note, if it is in one of `scopes` (any scope when None)."""
        async with self._session_factory() as db:
            row = await db.get(AgentMemory, note_id)
            if row is None or (scopes is not None and row.scope not in scopes):
                return None
            gone = Note.from_row(row)
            await db.delete(row)
            await db.commit()
        if self.knowledge.store is not None:
            try:
                await self.knowledge.store.delete(COLLECTION, [note_id])
            except VectorStoreError as exc:
                # Harmless: searches only return notes still in the table.
                log.warning("Could not delete the vector of note %d: %s", note_id, exc)
        return gone

    async def _embed_note(
        self, note: str, embedding: EmbeddingSettings
    ) -> tuple[list[float] | None, str]:
        vectors = self.knowledge.vectors_for(embedding)
        if vectors is None:
            return None, ""
        embedder, _ = vectors
        try:
            return (await embedder.embed_documents([note]))[0], embedder.tag
        except EmbeddingError as exc:
            log.warning("Could not embed a note, it will be retried: %s", exc)
            self._indexed_tag = None
            return None, ""

    async def _near_duplicate(self, scope: str, vector: list[float], tag: str) -> Note | None:
        store = self.knowledge.store
        assert store is not None
        try:
            hits = await store.search(
                COLLECTION,
                vector,
                limit=1,
                where={"scope": [scope], "model": [tag]},
                min_score=DUPLICATE_SCORE,
            )
        except VectorStoreError as exc:
            log.warning("Could not look for a similar note: %s", exc)
            return None
        if not hits:
            return None
        async with self._session_factory() as db:
            row = await db.get(AgentMemory, hits[0].id)
        return Note.from_row(row) if row is not None and row.scope == scope else None

    async def _store_vectors(
        self,
        items: list[tuple[int, str, list[float]]],
        embedding: EmbeddingSettings,
        tag: str,
    ) -> bool:
        """Put vectors in Qdrant and mark their notes; False if that failed."""
        store = self.knowledge.store
        assert store is not None
        try:
            if await store.ensure_collection(COLLECTION, len(items[0][2])):
                # A new collection: every other vector is gone with the old one.
                await self._mark_all_pending()
            await store.upsert(
                COLLECTION,
                [Point(note_id, vector, {"scope": scope, "model": tag}) for note_id, scope, vector in items],
            )
        except VectorStoreError as exc:
            log.warning("Could not store %d note vector(s), they will be retried: %s", len(items), exc)
            self._indexed_tag = None
            return False
        async with self._session_factory() as db:
            await db.execute(
                update(AgentMemory)
                .where(AgentMemory.id.in_([note_id for note_id, _, _ in items]))
                .values(embedded_model=tag)
            )
            await db.commit()
        return True

    async def _mark_all_pending(self) -> None:
        async with self._session_factory() as db:
            await db.execute(update(AgentMemory).values(embedded_model=""))
            await db.commit()
        self._indexed_tag = None

    # ----------------------------------------------------------------- #
    # Catching up on vectors
    # ----------------------------------------------------------------- #

    def ensure_indexed(self, embedding: EmbeddingSettings) -> None:
        """Embed, in the background, the notes the current model has no vector for."""
        tag = model_tag(embedding.model_id)
        if not tag or self.knowledge.store is None or self._indexed_tag == tag:
            return
        if self._index_task is not None and not self._index_task.done():
            return
        if time.monotonic() < self._index_retry_at:
            return
        self._index_task = asyncio.create_task(self._index(embedding), name="memory-index")

    async def _index(self, embedding: EmbeddingSettings) -> None:
        try:
            done = await self.index_pending(embedding)
            if done:
                log.info("Embedded %d memory note(s)", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retried on a later turn
            log.warning("Could not embed the pending memory notes", exc_info=True)
        if self._indexed_tag != model_tag(embedding.model_id):
            # Something failed; let a later turn try again, not the next one.
            self._index_retry_at = time.monotonic() + INDEX_RETRY_SECONDS

    async def index_pending(self, embedding: EmbeddingSettings) -> int:
        """Embed every note lacking a vector of the current model; how many were."""
        vectors = self.knowledge.vectors_for(embedding)
        if vectors is None:
            return 0
        embedder, _ = vectors
        tag = embedder.tag
        done = 0
        while True:
            async with self._session_factory() as db:
                rows = (
                    await db.scalars(
                        select(AgentMemory)
                        .where(AgentMemory.embedded_model != tag)
                        .order_by(AgentMemory.id)
                        .limit(INDEX_BATCH)
                    )
                ).all()
            if not rows:
                break
            try:
                made = await embedder.embed_documents([row.text for row in rows])
            except EmbeddingError as exc:
                log.warning("Embedding memory notes failed: %s", exc)
                return done
            items = [(row.id, row.scope, vector) for row, vector in zip(rows, made, strict=True)]
            if not await self._store_vectors(items, embedding, tag):
                return done
            done += len(rows)
        self._indexed_tag = tag
        return done

    async def pending_count(self, embedding: EmbeddingSettings) -> int:
        tag = model_tag(embedding.model_id)
        async with self._session_factory() as db:
            return await db.scalar(
                select(func.count()).select_from(AgentMemory).where(AgentMemory.embedded_model != tag)
            ) or 0

    # ----------------------------------------------------------------- #
    # Reading
    # ----------------------------------------------------------------- #

    async def recall(
        self,
        question: str,
        scopes: Sequence[str],
        *,
        embedding: EmbeddingSettings,
        limit: int,
        min_score: float,
    ) -> list[Note]:
        """The notes to show the model with `question`, the most relevant first.

        When the conversation can see no more notes than `limit`, all of them
        are shown, without a search.
        """
        if limit <= 0:
            return []
        async with self._session_factory() as db:
            rows = (
                await db.scalars(
                    select(AgentMemory)
                    .where(AgentMemory.scope.in_(scopes))
                    .order_by(AgentMemory.id)
                    .limit(limit + 1)
                )
            ).all()
        if len(rows) <= limit:
            return [Note.from_row(row) for row in rows]
        return await self.search(
            question, scopes, embedding=embedding, limit=limit, min_score=min_score
        )

    async def search(
        self,
        query: str,
        scopes: Sequence[str],
        *,
        embedding: EmbeddingSettings,
        limit: int = 5,
        min_score: float | None = None,
    ) -> list[Note]:
        """Notes of `scopes` matching `query` by meaning or by words."""
        dense, words = await asyncio.gather(
            self._dense(query, scopes, embedding, min_score), self._words(query, scopes)
        )
        scores = dict(rrf([dense, words]))
        merged = list(scores)[:CANDIDATES]
        if not merged:
            return []
        async with self._session_factory() as db:
            rows = (
                await db.scalars(
                    select(AgentMemory).where(
                        AgentMemory.id.in_(merged), AgentMemory.scope.in_(scopes)
                    )
                )
            ).all()
        by_id = {row.id: row for row in rows}
        notes = [Note.from_row(by_id[i], scores[i]) for i in merged if i in by_id]

        reranker = self.knowledge.reranker_for(embedding)
        if reranker is not None and len(notes) > 1:
            try:
                reranked = await reranker.rerank(query, [note.text for note in notes])
            except EmbeddingError as exc:
                log.warning("Reranking memory failed, keeping the merged order: %s", exc)
            else:
                for note, score in zip(notes, reranked, strict=True):
                    note.score = score
                notes.sort(key=lambda note: note.score or 0.0, reverse=True)
        return notes[:limit]

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
            log.warning("Searching memory by meaning failed, using words only: %s", exc)
            return []
        return [hit.id for hit in hits]

    async def _words(self, query: str, scopes: Sequence[str]) -> list[int]:
        match = fts_query(query)
        if match is None:
            return []
        statement = text(
            "SELECT m.id FROM agent_memory_fts JOIN agent_memory AS m "
            "ON m.id = agent_memory_fts.rowid "
            "WHERE agent_memory_fts MATCH :match AND m.scope IN :scopes "
            "ORDER BY bm25(agent_memory_fts) LIMIT :limit"
        ).bindparams(bindparam("scopes", expanding=True))
        async with self._session_factory() as db:
            rows = await db.execute(
                statement, {"match": match, "scopes": list(scopes), "limit": CANDIDATES}
            )
            return [row[0] for row in rows]

    async def list(
        self,
        *,
        scope: str | None = None,
        query: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Note], int]:
        """Notes for the portal, newest first, with how many match in all."""
        conditions = []
        if scope:
            conditions.append(AgentMemory.scope == scope)
        if query:
            pattern = f"%{query.strip()}%"
            conditions.append(
                or_(AgentMemory.text.ilike(pattern), AgentMemory.created_by.ilike(pattern))
            )
        async with self._session_factory() as db:
            total = await db.scalar(
                select(func.count()).select_from(AgentMemory).where(*conditions)
            )
            rows = (
                await db.scalars(
                    select(AgentMemory)
                    .where(*conditions)
                    .order_by(AgentMemory.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
        return [Note.from_row(row) for row in rows], total or 0

    async def scopes(self) -> list[tuple[str, int]]:
        async with self._session_factory() as db:
            rows = await db.execute(
                select(AgentMemory.scope, func.count())
                .group_by(AgentMemory.scope)
                .order_by(func.count().desc())
            )
            return [(scope, count) for scope, count in rows]

    async def forget_scope(self, scope: str) -> int:
        async with self._session_factory() as db:
            ids = list(
                (await db.scalars(select(AgentMemory.id).where(AgentMemory.scope == scope))).all()
            )
            await db.execute(delete(AgentMemory).where(AgentMemory.scope == scope))
            await db.commit()
        if ids and self.knowledge.store is not None:
            with contextlib.suppress(VectorStoreError):
                await self.knowledge.store.delete(COLLECTION, ids)
        return len(ids)

    async def close(self) -> None:
        if self._index_task is not None:
            self._index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._index_task
