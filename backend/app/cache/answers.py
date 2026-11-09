"""Answers given again, in well under a second, to questions asked before.

Two kinds of entry (`app.models.AnswerEntry`):

- **saved** ("auto"): after the assistant answers a question from the
  knowledge base alone (no other tool, no conversation documents, passages
  the model judged to answer it), the fast model rewrites the question to
  stand on its own and answers *that* from the passages only. The answer
  holds nothing of who asked or what was said before, so it can be given to
  anyone. It is dropped when the knowledge base changes, when it expires,
  and at the first 👎.
- **pinned**: written by an admin, or a saved answer an admin kept, with any
  number of ways to ask it. Given until changed or removed.

**Finding one** (`find`): the question with case and spacing folded is
looked up exactly first; then, with an embedding model, by meaning, among
the vectors of every way of asking (collection `answer_cache`). A saved
answer needs a very close match (`answer_min_similarity`, 0.95 by
default); a pinned one its own bar. Nothing here may stop an answer: on
any failure, the question goes the full way.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.bot.config_store import EmbeddingSettings, KnowledgeSettings
from app.bot.rcformat import strip_thinking
from app.knowledge.base import BASE_SCOPE, KnowledgeBase, Passage
from app.knowledge.embeddings import EmbeddingError, model_tag
from app.knowledge.service import KnowledgeService
from app.knowledge.vectorstore import Point, VectorStoreError
from app.models import AnswerEntry, AnswerKey, AnswerKind, utcnow

if TYPE_CHECKING:
    from app.bot.llm import LLMChat

log = logging.getLogger(__name__)

COLLECTION = "answer_cache"
# Vector matches looked at for one question.
CANDIDATES = 5
INDEX_BATCH = 64
LEARN_MAX_TOKENS = 1500
LEARN_TIMEOUT_SECONDS = 60.0
# Messages of the conversation shown when rewriting the question.
LEARN_HISTORY = 4
MAX_QUESTION_CHARS = 500
MAX_ANSWER_CHARS = 6000
MAX_KEYS = 20

LEARN_PROMPT = """You prepare an answer to keep and give again to anyone who asks the same question.
You get the recent conversation, the user's latest question, and numbered passages
from the knowledge base.

1. Rewrite the latest question so it stands on its own: resolve words like "it",
   "that", "còn ... thì sao" from the conversation. Keep the user's language.
2. Decide whether the answer suits anyone who asks. It does not if the question is
   about the user personally or their own situation, depends on today's date or
   time, asks for an opinion, or if the passages do not fully answer it.
3. If it does, answer the rewritten question from the passages only: concisely, in
   the question's language, citing passages as [1], [2]. Nothing about the user.

Reply with JSON only: {"reusable": true, "question": "...", "answer": "..."}
or {"reusable": false}."""


def key_hash(text: str) -> str:
    """How a question is matched exactly: case and spacing do not matter."""
    folded = " ".join(text.casefold().split()).rstrip("?.!… ")
    return hashlib.sha256(folded.encode()).hexdigest()


@dataclass(slots=True)
class Source:
    number: int
    label: str


@dataclass(slots=True)
class CachedAnswer:
    entry_id: int
    kind: str
    question: str
    answer: str
    sources: list[Source] = field(default_factory=list)
    # "exact", or "meaning" with the similarity.
    matched_by: str = "exact"
    score: float | None = None

    def render(self) -> str:
        """The answer as posted, with where it comes from."""
        parts = []
        if self.sources:
            parts.append("Sources: " + " · ".join(f"[{s.number}] {s.label}" for s in self.sources))
        if self.kind == AnswerKind.pinned.value:
            parts.insert(0, ":zap: FAQ")
        else:
            parts.insert(0, ":zap: Saved answer")
            parts.append("react :thumbsdown: for a fresh one")
        return f"{self.answer}\n\n_{' · '.join(parts)}_"


class AnswerError(ValueError):
    """An entry was refused; the message says why."""


def _sources_from_json(raw: str) -> list[Source]:
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("label"):
            out.append(Source(int(item.get("number") or len(out) + 1), str(item["label"])))
    return out


def _sources_json(sources: Sequence[Source]) -> str:
    return json.dumps([{"number": s.number, "label": s.label} for s in sources], ensure_ascii=False)


_CITATION = re.compile(r"\[(\d{1,2})\]")


def cited_sources(answer: str, passages: Sequence[Passage]) -> list[Source]:
    """The passages `answer` cites ([1], [2], ...), as sources."""
    numbers = sorted({int(n) for n in _CITATION.findall(answer) if 1 <= int(n) <= len(passages)})
    return [Source(n, passages[n - 1].label) for n in numbers]


def _json_object(raw: str) -> dict | None:
    raw = strip_thinking(raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean_keys(question: str, alternatives: Sequence[str]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for text in [question, *alternatives]:
        text = " ".join(text.split())[:MAX_QUESTION_CHARS]
        digest = key_hash(text) if text else ""
        if text and digest not in seen:
            seen.add(digest)
            keys.append(text)
    return keys[:MAX_KEYS]


class AnswerCache:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        knowledge: KnowledgeService,
        kb: KnowledgeBase,
    ) -> None:
        self._session_factory = session_factory
        self.knowledge = knowledge
        self.kb = kb
        self._indexed_tag: str | None = None
        self._index_task: asyncio.Task | None = None
        # Set when keys were added while an embedding run was under way.
        self._index_again = False
        self.served = 0

    # ----------------------------------------------------------------- #
    # Finding an answer
    # ----------------------------------------------------------------- #

    async def find(
        self,
        question: str,
        *,
        embedding: EmbeddingSettings,
        settings: KnowledgeSettings,
        record: bool = True,
    ) -> CachedAnswer | None:
        """The answer kept for `question`, or None; never raises.

        `record` counts it as given (False when an admin only tries a question).
        """
        try:
            return await self._find(question, embedding, settings, record)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the question goes the full way
            log.warning("Could not look %r up in the saved answers", question[:80], exc_info=True)
            return None

    async def _find(
        self,
        question: str,
        embedding: EmbeddingSettings,
        settings: KnowledgeSettings,
        record: bool = False,
    ) -> CachedAnswer | None:
        if not question.strip():
            return None
        async with self._session_factory() as db:
            exact = (
                await db.scalars(
                    select(AnswerEntry)
                    .join(AnswerKey, AnswerKey.entry_id == AnswerEntry.id)
                    .where(AnswerKey.text_hash == key_hash(question))
                    # Pinned first.
                    .order_by(AnswerEntry.kind.desc(), AnswerEntry.id.desc())
                )
            ).all()
        for entry in exact:
            if await self._usable(entry, settings):
                return await self._served(entry, "exact", None, record)

        vectors = self.knowledge.vectors_for(embedding)
        if vectors is None:
            return None
        embedder, store = vectors
        bar = min(settings.answer_min_similarity, settings.pinned_min_similarity)
        try:
            vector = await embedder.embed_query(question)
            hits = await store.search(
                COLLECTION,
                vector,
                limit=CANDIDATES,
                where={"scope": [BASE_SCOPE], "model": [embedder.tag]},
                min_score=bar,
            )
        except (EmbeddingError, VectorStoreError) as exc:
            log.info("Could not match the question against saved answers by meaning: %s", exc)
            return None
        for hit in hits:
            pinned = hit.payload.get("kind") == AnswerKind.pinned.value
            needed = settings.pinned_min_similarity if pinned else settings.answer_min_similarity
            if hit.score < needed:
                continue
            async with self._session_factory() as db:
                entry = await db.get(AnswerEntry, int(hit.payload.get("entry_id", 0)))
            if entry is not None and await self._usable(entry, settings):
                return await self._served(entry, "meaning", hit.score, record)
        return None

    async def _usable(self, entry: AnswerEntry, settings: KnowledgeSettings) -> bool:
        """Whether `entry` may be given now; a saved answer gone stale is dropped."""
        if entry.kind == AnswerKind.pinned.value:
            return True
        if not settings.answer_cache:
            return False
        expires = entry.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=utcnow().tzinfo)
        stale = (expires is not None and expires <= utcnow()) or (
            entry.kb_version != await self.kb.base_version()
        )
        if stale:
            await self.drop(entry.id)
            log.info("Dropped saved answer %d: %s", entry.id, "expired" if expires and expires <= utcnow() else "the knowledge base changed")
            return False
        return True

    async def _served(
        self, entry: AnswerEntry, matched_by: str, score: float | None, record: bool
    ) -> CachedAnswer:
        if record:
            async with self._session_factory() as db:
                await db.execute(
                    update(AnswerEntry)
                    .where(AnswerEntry.id == entry.id)
                    .values(hits=AnswerEntry.hits + 1, last_hit_at=utcnow())
                )
                await db.commit()
            self.served += 1
        return CachedAnswer(
            entry.id,
            entry.kind,
            entry.question,
            entry.answer,
            _sources_from_json(entry.sources_json),
            matched_by,
            score,
        )

    # ----------------------------------------------------------------- #
    # Saving answers
    # ----------------------------------------------------------------- #

    async def learn(
        self,
        chat: LLMChat,
        *,
        question: str,
        history: Sequence[dict],
        passages: Sequence[Passage],
        kb_version: str,
        settings: KnowledgeSettings,
        embedding: EmbeddingSettings,
        model: str | None = None,
        created_by: str | None = None,
    ) -> int | None:
        """Save an answer to `question` that anyone can be given; its id, or None.

        The fast model writes it from the passages alone, after rewriting
        the question to stand on its own.
        """
        try:
            return await self._learn(
                chat, question, history, passages, kb_version, settings, embedding, model, created_by
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - nothing is saved this time
            log.warning("Could not save an answer to %r", question[:80], exc_info=True)
            return None

    async def _learn(
        self,
        chat: LLMChat,
        question: str,
        history: Sequence[dict],
        passages: Sequence[Passage],
        kb_version: str,
        settings: KnowledgeSettings,
        embedding: EmbeddingSettings,
        model: str | None,
        created_by: str | None,
    ) -> int | None:
        said = "\n".join(
            f"{m.get('role')}: {str(m.get('content') or '')[:600]}" for m in history[-LEARN_HISTORY:]
        )
        listing = "\n\n".join(
            f"[{n}] {p.label}\n{p.shown}" for n, p in enumerate(passages, start=1)
        )
        async with asyncio.timeout(LEARN_TIMEOUT_SECONDS):
            raw = await chat.complete(
                [
                    {"role": "system", "content": LEARN_PROMPT},
                    {
                        "role": "user",
                        "content": f"Conversation:\n{said or '(none)'}\n\n"
                        f"Latest question: {question}\n\nPassages:\n\n{listing}",
                    },
                ],
                model=model or None,
                max_tokens=LEARN_MAX_TOKENS,
                quick=True,
            )
        data = _json_object(raw)
        if not data or data.get("reusable") is not True:
            log.info("Not saving an answer to %r: not reusable", question[:80])
            return None
        standalone = " ".join(str(data.get("question") or "").split())[:MAX_QUESTION_CHARS]
        answer = str(data.get("answer") or "").strip()[:MAX_ANSWER_CHARS]
        if not standalone or not answer:
            return None
        sources = cited_sources(answer, passages)
        if not sources:
            # An answer that cites nothing is not tied to the passages.
            log.info("Not saving an answer to %r: it cites no passage", standalone[:80])
            return None
        if kb_version != await self.kb.base_version():
            return None  # the documents changed while it was written
        known = await self._find(standalone, embedding, settings)
        if known is not None:
            return known.entry_id
        hours = max(1, settings.answer_ttl_hours)
        entry = await self._add(
            AnswerKind.auto.value,
            standalone,
            answer,
            [standalone],
            sources=sources,
            kb_version=kb_version,
            expires_at=utcnow() + timedelta(hours=hours),
            created_by=created_by,
        )
        log.info("Saved answer %d to %r", entry, standalone[:80])
        self.ensure_indexed(embedding)
        return entry

    async def _add(
        self,
        kind: str,
        question: str,
        answer: str,
        keys: Sequence[str],
        *,
        sources: Sequence[Source] = (),
        kb_version: str = "",
        expires_at: datetime | None = None,
        created_by: str | None = None,
    ) -> int:
        async with self._session_factory() as db:
            entry = AnswerEntry(
                kind=kind,
                question=question,
                answer=answer,
                sources_json=_sources_json(sources),
                scope=BASE_SCOPE,
                kb_version=kb_version,
                expires_at=expires_at,
                hits=0,
                downvotes=0,
                created_by=created_by,
            )
            entry.keys = [AnswerKey(text=k, text_hash=key_hash(k), embedded_model="") for k in keys]
            db.add(entry)
            await db.commit()
            return entry.id

    # ----------------------------------------------------------------- #
    # Admin: pinned answers, listing, removing
    # ----------------------------------------------------------------- #

    async def pin(
        self,
        question: str,
        answer: str,
        alternatives: Sequence[str] = (),
        *,
        embedding: EmbeddingSettings,
        created_by: str | None = None,
    ) -> int:
        keys = _clean_keys(question, alternatives)
        if not keys or not answer.strip():
            raise AnswerError("a pinned answer needs a question and an answer")
        entry = await self._add(
            AnswerKind.pinned.value, keys[0], answer.strip(), keys, created_by=created_by
        )
        self._indexed_tag = None
        self.ensure_indexed(embedding)
        return entry

    async def update_pinned(
        self,
        entry_id: int,
        *,
        question: str | None = None,
        answer: str | None = None,
        alternatives: Sequence[str] | None = None,
        embedding: EmbeddingSettings,
    ) -> bool:
        """Change a pinned answer (or keep a saved one: it becomes pinned)."""
        async with self._session_factory() as db:
            entry = await db.get(AnswerEntry, entry_id, options=[selectinload(AnswerEntry.keys)])
            if entry is None:
                return False
            old_keys = [key.id for key in entry.keys]
            first = (question if question is not None else entry.question).strip()
            others = (
                list(alternatives)
                if alternatives is not None
                else [key.text for key in entry.keys[1:]]
            )
            keys = _clean_keys(first, others)
            text = (answer if answer is not None else entry.answer).strip()
            if not keys or not text:
                raise AnswerError("a pinned answer needs a question and an answer")
            entry.kind = AnswerKind.pinned.value
            entry.question = keys[0]
            entry.answer = text[:MAX_ANSWER_CHARS * 2]
            entry.expires_at = None
            entry.keys = [AnswerKey(text=k, text_hash=key_hash(k), embedded_model="") for k in keys]
            await db.commit()
        await self._drop_vectors(old_keys)
        self._indexed_tag = None
        self.ensure_indexed(embedding)
        return True

    async def entries(self, kind: str | None = None, limit: int = 500) -> list[AnswerEntry]:
        async with self._session_factory() as db:
            statement = select(AnswerEntry).options(selectinload(AnswerEntry.keys))
            if kind:
                statement = statement.where(AnswerEntry.kind == kind)
            statement = statement.order_by(AnswerEntry.kind.desc(), AnswerEntry.id.desc()).limit(limit)
            return list((await db.scalars(statement)).all())

    async def counts(self) -> dict[str, int]:
        async with self._session_factory() as db:
            rows = await db.execute(select(AnswerEntry.kind, func.count()).group_by(AnswerEntry.kind))
            return {kind: count for kind, count in rows}

    async def drop(self, entry_id: int) -> bool:
        async with self._session_factory() as db:
            keys = list(
                (await db.scalars(select(AnswerKey.id).where(AnswerKey.entry_id == entry_id))).all()
            )
            await db.execute(delete(AnswerKey).where(AnswerKey.entry_id == entry_id))
            gone = (await db.execute(delete(AnswerEntry).where(AnswerEntry.id == entry_id))).rowcount
            await db.commit()
        await self._drop_vectors(keys)
        return bool(gone)

    async def clear(self, kind: str = AnswerKind.auto.value) -> int:
        """Remove every entry of a kind; how many there were."""
        async with self._session_factory() as db:
            ids = list((await db.scalars(select(AnswerEntry.id).where(AnswerEntry.kind == kind))).all())
            keys = list(
                (await db.scalars(select(AnswerKey.id).where(AnswerKey.entry_id.in_(ids)))).all()
            )
            await db.execute(delete(AnswerKey).where(AnswerKey.entry_id.in_(ids)))
            await db.execute(delete(AnswerEntry).where(AnswerEntry.id.in_(ids)))
            await db.commit()
        await self._drop_vectors(keys)
        return len(ids)

    async def downvote(self, entry_id: int) -> str | None:
        """Someone reacted 👎: a saved answer goes, a pinned one is counted. Its kind, or None."""
        async with self._session_factory() as db:
            entry = await db.get(AnswerEntry, entry_id)
            if entry is None:
                return None
            kind = entry.kind
            if kind == AnswerKind.pinned.value:
                entry.downvotes += 1
                await db.commit()
                return kind
        await self.drop(entry_id)
        log.info("Dropped saved answer %d after a 👎", entry_id)
        return kind

    async def _drop_vectors(self, key_ids: list[int]) -> None:
        if key_ids and self.knowledge.store is not None:
            try:
                await self.knowledge.store.delete(COLLECTION, key_ids)
            except VectorStoreError as exc:
                # Harmless: a match is only used if its entry still exists.
                log.warning("Could not delete %d answer vectors: %s", len(key_ids), exc)

    # ----------------------------------------------------------------- #
    # Vectors
    # ----------------------------------------------------------------- #

    def ensure_indexed(self, embedding: EmbeddingSettings) -> None:
        """Embed, in the background, the ways of asking that have no vector yet."""
        tag = model_tag(embedding.model_id)
        if not tag or self.knowledge.store is None or self._indexed_tag == tag:
            return
        if self._index_task is not None and not self._index_task.done():
            self._index_again = True
            return
        self._index_task = asyncio.create_task(self._index(embedding), name="answer-index")

    async def _index(self, embedding: EmbeddingSettings) -> None:
        try:
            done = 0
            while True:
                self._index_again = False
                done += await self.index_pending(embedding)
                if not self._index_again:
                    break
            if done:
                log.info("Embedded %d way(s) of asking saved answers", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - tried again on a later turn
            log.warning("Could not embed the saved answers' questions", exc_info=True)

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
                        select(AnswerKey, AnswerEntry.kind, AnswerEntry.scope)
                        .join(AnswerEntry, AnswerEntry.id == AnswerKey.entry_id)
                        .where(AnswerKey.embedded_model != tag)
                        .order_by(AnswerKey.id)
                        .limit(INDEX_BATCH)
                    )
                ).all()
            if not rows:
                break
            try:
                made = await embedder.embed_queries([key.text for key, _, _ in rows])
                if await store.ensure_collection(COLLECTION, len(made[0])):
                    async with self._session_factory() as db:
                        await db.execute(update(AnswerKey).values(embedded_model=""))
                        await db.commit()
                await store.upsert(
                    COLLECTION,
                    [
                        Point(
                            key.id,
                            vector,
                            {"entry_id": key.entry_id, "kind": kind, "scope": scope, "model": tag},
                        )
                        for (key, kind, scope), vector in zip(rows, made, strict=True)
                    ],
                )
            except (EmbeddingError, VectorStoreError) as exc:
                log.warning("Embedding the saved answers' questions failed: %s", exc)
                return done
            async with self._session_factory() as db:
                await db.execute(
                    update(AnswerKey)
                    .where(AnswerKey.id.in_([key.id for key, _, _ in rows]))
                    .values(embedded_model=tag)
                )
                await db.commit()
            done += len(rows)
        self._indexed_tag = tag
        return done

    async def close(self) -> None:
        if self._index_task is not None:
            self._index_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._index_task
