"""Kept answers: saved after the assistant answers from the knowledge base,
or pinned by admins (FAQ), and given again at once to the same question.

Also how long the in-memory caches keep things, and how often they helped.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.api.memory import _agent
from app.bot.config_store import bump_config_version, ensure_knowledge_config
from app.cache.answers import AnswerCache, AnswerError
from app.knowledge.embeddings import model_tag
from app.models import AnswerEntry, AnswerKind, KnowledgeConfig
from app.runtime import Runtime
from app.schemas import (
    AnswerEntryIn,
    AnswerEntryOut,
    AnswerEntryUpdate,
    AnswerSourceOut,
    CacheConfigOut,
    CacheConfigUpdate,
    CacheStatsOut,
    CacheTestOut,
)

router = APIRouter(prefix="/api/cache", tags=["cache"])

CONFIG_FIELDS = ("answer_cache", "answer_min_similarity", "pinned_min_similarity", "answer_ttl_hours")


def _answers(runtime: Runtime) -> AnswerCache:
    return _agent(runtime).answers


def _config_out(config: KnowledgeConfig) -> CacheConfigOut:
    return CacheConfigOut(**{field: getattr(config, field) for field in CONFIG_FIELDS})


def _entry_out(entry: AnswerEntry, tag: str) -> AnswerEntryOut:
    try:
        sources = json.loads(entry.sources_json or "[]")
    except ValueError:
        sources = []
    keys = list(entry.keys)
    return AnswerEntryOut(
        id=entry.id,
        kind=entry.kind,  # type: ignore[arg-type]
        question=entry.question,
        alternatives=[key.text for key in keys[1:]],
        answer=entry.answer,
        sources=[AnswerSourceOut(number=s["number"], label=s["label"]) for s in sources if isinstance(s, dict)],
        hits=entry.hits,
        downvotes=entry.downvotes,
        last_hit_at=entry.last_hit_at,
        expires_at=entry.expires_at,
        created_by=entry.created_by,
        created_at=entry.created_at,
        embedded=bool(tag) and all(key.embedded_model == tag for key in keys),
    )


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


@router.get("/config", response_model=CacheConfigOut)
async def read_cache_config(_: CurrentUser, session: SessionDep) -> CacheConfigOut:
    config = await ensure_knowledge_config(session)
    await session.commit()
    return _config_out(config)


@router.put("/config", response_model=CacheConfigOut)
async def update_cache_config(
    payload: CacheConfigUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> CacheConfigOut:
    config = await ensure_knowledge_config(session)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in changes.items():
        setattr(config, field, value)
    await bump_config_version(session)
    record_audit(
        session, admin, "update", "cache_config", config.id, ", ".join(sorted(changes)) or "(no changes)"
    )
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    return _config_out(config)


@router.get("/stats", response_model=CacheStatsOut)
async def cache_stats(_: CurrentUser, runtime: RuntimeDep) -> CacheStatsOut:
    agent = _agent(runtime)
    counts = await agent.answers.counts()
    return CacheStatsOut(
        saved=counts.get(AnswerKind.auto.value, 0),
        pinned=counts.get(AnswerKind.pinned.value, 0),
        served=agent.answers.served,
        lookups=agent.kb.results.stats(),
        tool_results=agent.tool_results.stats(),
    )


@router.get("/answers", response_model=list[AnswerEntryOut])
async def list_answers(
    _: CurrentUser,
    runtime: RuntimeDep,
    kind: AnswerKind | None = Query(default=None),
) -> list[AnswerEntryOut]:
    snapshot = await runtime.config_store.current()
    tag = model_tag(snapshot.embedding.model_id)
    entries = await _answers(runtime).entries(kind.value if kind else None)
    return [_entry_out(entry, tag) for entry in entries]


async def _one(runtime: Runtime, entry_id: int) -> AnswerEntryOut:
    snapshot = await runtime.config_store.current()
    for entry in await _answers(runtime).entries():
        if entry.id == entry_id:
            return _entry_out(entry, model_tag(snapshot.embedding.model_id))
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such answer")


@router.post("/answers", response_model=AnswerEntryOut, status_code=status.HTTP_201_CREATED)
async def pin_answer(
    payload: AnswerEntryIn, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> AnswerEntryOut:
    snapshot = await runtime.config_store.current()
    try:
        entry_id = await _answers(runtime).pin(
            payload.question,
            payload.answer,
            payload.alternatives,
            embedding=snapshot.embedding,
            created_by=admin.email,
        )
    except AnswerError as exc:
        raise _unprocessable(str(exc)) from exc
    record_audit(session, admin, "create", "answer_entry", entry_id, payload.question[:200])
    await session.commit()
    return await _one(runtime, entry_id)


@router.put("/answers/{entry_id}", response_model=AnswerEntryOut)
async def update_answer(
    entry_id: int,
    payload: AnswerEntryUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> AnswerEntryOut:
    """Change a pinned answer; a saved one becomes pinned (kept until removed)."""
    snapshot = await runtime.config_store.current()
    try:
        found = await _answers(runtime).update_pinned(
            entry_id,
            question=payload.question,
            answer=payload.answer,
            alternatives=payload.alternatives,
            embedding=snapshot.embedding,
        )
    except AnswerError as exc:
        raise _unprocessable(str(exc)) from exc
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such answer")
    changed = ", ".join(sorted(payload.model_dump(exclude_unset=True))) or "pinned"
    record_audit(session, admin, "update", "answer_entry", entry_id, changed)
    await session.commit()
    return await _one(runtime, entry_id)


@router.delete("/answers/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_answer(
    entry_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    if not await _answers(runtime).drop(entry_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such answer")
    record_audit(session, admin, "delete", "answer_entry", entry_id, "")
    await session.commit()


class ClearResult(BaseModel):
    removed: int


@router.delete("/answers", response_model=ClearResult)
async def clear_answers(
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
    kind: AnswerKind = Query(default=AnswerKind.auto),
) -> ClearResult:
    """Remove every saved answer (or, asked for, every pinned one)."""
    removed = await _answers(runtime).clear(kind.value)
    record_audit(session, admin, "delete", "answer_entry", None, f"all {kind.value}: {removed}")
    await session.commit()
    return ClearResult(removed=removed)


class CacheTestIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


@router.post("/test", response_model=CacheTestOut)
async def test_question(payload: CacheTestIn, _: CurrentUser, runtime: RuntimeDep) -> CacheTestOut:
    """What the question would be given from kept answers, without counting it."""
    snapshot = await runtime.config_store.current()
    found = await _answers(runtime).find(
        payload.question,
        embedding=snapshot.embedding,
        settings=snapshot.knowledge,
        record=False,
    )
    if found is None:
        return CacheTestOut(found=False)
    return CacheTestOut(
        found=True,
        entry_id=found.entry_id,
        kind=found.kind,
        matched_by=found.matched_by,
        score=found.score,
        reply=found.render(),
    )
