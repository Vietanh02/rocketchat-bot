"""The assistant's memory: how it searches (embedding model, reranker, vector
database), what it remembers (notes) and who it talks to (profiles).

Notes and profiles hold what people told the bot, so only admins read them.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.agent.memory import GLOBAL_SCOPE, MemoryStore, Note, NoteError
from app.agent.profiles import ProfileError, ProfileStore
from app.agent.runner import AgentRunner
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.bot.config_store import (
    EmbeddingSettings,
    bump_config_version,
    ensure_embedding_config,
    ensure_memory_config,
)
from app.integrations.executor import IntegrationError, assert_url_allowed
from app.knowledge.embeddings import Embedder, EmbeddingError, Reranker
from app.knowledge.vectorstore import VectorStoreError
from app.models import AgentMemory, AgentUserProfile, EmbeddingConfig, MemoryConfig
from app.runtime import Runtime
from app.schemas import (
    EmbeddingConfigOut,
    EmbeddingConfigUpdate,
    EmbeddingTestResult,
    MemoryConfigOut,
    MemoryConfigUpdate,
    MemoryNoteCreate,
    MemoryNoteList,
    MemoryNoteOut,
    MemoryNoteUpdate,
    MemoryScopeOut,
    MemoryStatusOut,
    ProfileOut,
    ProfileUpdate,
    VectorDbStatus,
)
from app.security import encrypt_secret

router = APIRouter(prefix="/api/memory", tags=["memory"])

EMBEDDING_FIELDS = (
    "provider",
    "base_url",
    "model",
    "query_prefix",
    "document_prefix",
    "timeout_seconds",
    "rerank_provider",
    "rerank_base_url",
    "rerank_model",
)
TEST_TEXTS = ("Rocket.Chat is a chat server.", "Hôm nay trời đẹp.")


def _agent(runtime: Runtime) -> AgentRunner:
    agent = getattr(runtime.bot.responder, "agent", None)
    if agent is None:  # pragma: no cover - every responder in use has one
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="No assistant")
    return agent


def _memory(runtime: Runtime) -> MemoryStore:
    return _agent(runtime).memory


def _profiles(runtime: Runtime) -> ProfileStore:
    return _agent(runtime).profiles


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


# --------------------------------------------------------------------------- #
# Embedding model and reranker
# --------------------------------------------------------------------------- #


def _embedding_out(config: EmbeddingConfig, runtime: Runtime) -> EmbeddingConfigOut:
    settings = EmbeddingSettings.from_model(config)
    return EmbeddingConfigOut(
        provider=config.provider or "",
        base_url=config.base_url,
        model=config.model,
        dimensions=config.dimensions,
        query_prefix=config.query_prefix,
        document_prefix=config.document_prefix,
        timeout_seconds=config.timeout_seconds,
        has_api_key=bool(config.api_key_enc),
        rerank_provider=config.rerank_provider or "",
        rerank_base_url=config.rerank_base_url,
        rerank_model=config.rerank_model,
        has_rerank_api_key=bool(config.rerank_api_key_enc),
        configured=settings.configured,
        rerank_configured=settings.rerank_configured,
        vector_db_configured=_agent(runtime).knowledge.store is not None,
    )


async def _check_url(url: str, what: str) -> None:
    try:
        await assert_url_allowed(url)
    except IntegrationError as exc:
        raise _unprocessable(f"{what}: {exc}") from exc


@router.get("/embedding", response_model=EmbeddingConfigOut)
async def read_embedding_config(
    _: CurrentUser, session: SessionDep, runtime: RuntimeDep
) -> EmbeddingConfigOut:
    config = await ensure_embedding_config(session)
    await session.commit()
    return _embedding_out(config, runtime)


@router.put("/embedding", response_model=EmbeddingConfigOut)
async def update_embedding_config(
    payload: EmbeddingConfigUpdate,
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
) -> EmbeddingConfigOut:
    config = await ensure_embedding_config(session)
    changes = payload.model_dump(exclude_unset=True)
    for field, what in (("base_url", "Embedding URL"), ("rerank_base_url", "Reranker URL")):
        if changes.get(field) and changes[field] != getattr(config, field):
            await _check_url(changes[field], what)

    for field in EMBEDDING_FIELDS:
        if field in changes and changes[field] is not None:
            setattr(config, field, changes[field])
    if "dimensions" in changes:
        config.dimensions = changes["dimensions"]
    if "api_key" in changes:
        config.api_key_enc = encrypt_secret((changes["api_key"] or "").strip() or None)
    if "rerank_api_key" in changes:
        config.rerank_api_key_enc = encrypt_secret((changes["rerank_api_key"] or "").strip() or None)

    await bump_config_version(session)
    record_audit(
        session,
        admin,
        "update",
        "embedding_config",
        config.id,
        # Field names only; keys never reach the audit log.
        ", ".join(sorted(changes)) or "(no changes)",
    )
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    # A new model needs every note embedded again; start on it now.
    snapshot = await runtime.config_store.current()
    _memory(runtime).ensure_indexed(snapshot.embedding)
    return _embedding_out(config, runtime)


@router.post("/embedding/test", response_model=EmbeddingTestResult)
async def test_embedding(
    payload: EmbeddingConfigUpdate, _: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> EmbeddingTestResult:
    """Embed two sentences (and rerank them) with these settings, without saving."""
    stored = EmbeddingSettings.from_model(await ensure_embedding_config(session))
    await session.commit()
    fields = payload.model_dump(exclude_unset=True)
    for secret in ("api_key", "rerank_api_key"):
        if not (fields.get(secret) or "").strip():
            fields.pop(secret, None)
    settings = EmbeddingSettings(
        **{
            name: fields.get(name, getattr(stored, name))
            for name in EmbeddingSettings.__dataclass_fields__
        }
    )
    if not settings.configured:
        return EmbeddingTestResult(ok=False, message="Choose a provider and set its URL and model.")

    http = _agent(runtime).knowledge.http
    started = time.monotonic()
    try:
        vectors = await Embedder(settings, http).embed_documents(list(TEST_TEXTS))
    except EmbeddingError as exc:
        return EmbeddingTestResult(
            ok=False,
            message=str(exc)[:1].upper() + str(exc)[1:],
            embed_ms=int((time.monotonic() - started) * 1000),
        )
    embed_ms = int((time.monotonic() - started) * 1000)
    result = EmbeddingTestResult(
        ok=True,
        message=f"{settings.model or 'The server'} made {len(vectors[0])}-dimension vectors "
        f"in {embed_ms} ms",
        dimensions=len(vectors[0]),
        embed_ms=embed_ms,
    )
    if settings.rerank_configured:
        started = time.monotonic()
        try:
            scores = await Reranker(settings, http).rerank("What is Rocket.Chat?", list(TEST_TEXTS))
        except EmbeddingError as exc:
            result.rerank_ok, result.rerank_message = False, str(exc)
        else:
            result.rerank_ok = scores[0] > scores[1]
            result.rerank_message = (
                "The reranker put the matching sentence first"
                if result.rerank_ok
                else "The reranker answered, but ranked the unrelated sentence first"
            )
        result.rerank_ms = int((time.monotonic() - started) * 1000)
    return result


# --------------------------------------------------------------------------- #
# Memory settings and state
# --------------------------------------------------------------------------- #


def _memory_out(config: MemoryConfig) -> MemoryConfigOut:
    return MemoryConfigOut(
        enabled=config.enabled,
        auto_extract=config.auto_extract,
        prompt_notes=config.prompt_notes,
        min_score=config.min_score,
    )


@router.get("/config", response_model=MemoryConfigOut)
async def read_memory_config(_: CurrentUser, session: SessionDep) -> MemoryConfigOut:
    config = await ensure_memory_config(session)
    await session.commit()
    return _memory_out(config)


@router.put("/config", response_model=MemoryConfigOut)
async def update_memory_config(
    payload: MemoryConfigUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> MemoryConfigOut:
    config = await ensure_memory_config(session)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    for field, value in changes.items():
        setattr(config, field, value)
    await bump_config_version(session)
    record_audit(
        session, admin, "update", "memory_config", config.id, ", ".join(sorted(changes)) or "(no changes)"
    )
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    return _memory_out(config)


@router.get("/status", response_model=MemoryStatusOut)
async def memory_status(_: CurrentUser, session: SessionDep, runtime: RuntimeDep) -> MemoryStatusOut:
    snapshot = await runtime.config_store.current()
    memory = _memory(runtime)
    store = memory.knowledge.store
    vector_db = VectorDbStatus(configured=store is not None)
    if store is not None:
        try:
            vector_db.version = await store.ping()
            vector_db.reachable = True
            vector_db.memory_vectors = await store.count("memory")
        except VectorStoreError as exc:
            vector_db.error = str(exc)
    notes = await session.scalar(select(func.count()).select_from(AgentMemory)) or 0
    embedding_on = snapshot.embedding.configured
    return MemoryStatusOut(
        vector_db=vector_db,
        embedding_configured=embedding_on,
        notes=notes,
        pending=await memory.pending_count(snapshot.embedding) if embedding_on else notes,
        semantic=embedding_on and vector_db.reachable,
    )


@router.post("/reindex", response_model=MemoryStatusOut)
async def reindex_memory(
    admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> MemoryStatusOut:
    """Embed now every note that has no vector of the current model."""
    snapshot = await runtime.config_store.current()
    if not snapshot.embedding.configured:
        raise _unprocessable("Set up an embedding model first.")
    memory = _memory(runtime)
    if memory.knowledge.store is None:
        raise _unprocessable("No vector database: set APP_QDRANT_URL and restart the backend.")
    try:
        await memory.index_pending(snapshot.embedding)
    except (EmbeddingError, VectorStoreError) as exc:
        raise _unprocessable(str(exc)) from exc
    record_audit(session, admin, "reindex", "agent_memory", "all", "")
    await session.commit()
    return await memory_status(admin, session, runtime)


# --------------------------------------------------------------------------- #
# Notes
# --------------------------------------------------------------------------- #


def _note_out(note: Note) -> MemoryNoteOut:
    return MemoryNoteOut(
        id=note.id,
        scope=note.scope,
        text=note.text,
        source=note.source,
        created_by=note.created_by,
        created_at=note.created_at,
        score=None if note.score is None else round(note.score, 4),
    )


@router.get("/notes", response_model=MemoryNoteList)
async def list_notes(
    _: AdminUser,
    runtime: RuntimeDep,
    scope: str | None = Query(default=None, max_length=160),
    q: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> MemoryNoteList:
    notes, total = await _memory(runtime).list(scope=scope, query=q, offset=offset, limit=limit)
    return MemoryNoteList(items=[_note_out(note) for note in notes], total=total)


@router.get("/search", response_model=list[MemoryNoteOut])
async def search_notes(
    _: AdminUser,
    runtime: RuntimeDep,
    q: str = Query(min_length=1, max_length=500),
    scope: list[str] = Query(default_factory=lambda: [GLOBAL_SCOPE]),
    limit: int = Query(default=10, ge=1, le=30),
) -> list[MemoryNoteOut]:
    """What a conversation reading `scope` would find for `q`, to tune the search."""
    snapshot = await runtime.config_store.current()
    notes = await _memory(runtime).search(
        q, scope, embedding=snapshot.embedding, limit=limit, min_score=snapshot.memory.min_score
    )
    return [_note_out(note) for note in notes]


@router.get("/scopes", response_model=list[MemoryScopeOut])
async def list_scopes(_: AdminUser, session: SessionDep, runtime: RuntimeDep) -> list[MemoryScopeOut]:
    scopes = await _memory(runtime).scopes()
    user_ids = [scope.split(":", 1)[1] for scope, _ in scopes if scope.startswith("user:")]
    names: dict[str, str] = {}
    if user_ids:
        rows = await session.execute(
            select(AgentUserProfile.rc_user_id, AgentUserProfile.username).where(
                AgentUserProfile.rc_user_id.in_(user_ids)
            )
        )
        names.update({user_id: username for user_id, username in rows if username})
        missing = [user_id for user_id in user_ids if user_id not in names]
        if missing:
            # Whoever the notes were learnt from, in that person's DM.
            rows = await session.execute(
                select(AgentMemory.scope, func.max(AgentMemory.created_by))
                .where(AgentMemory.scope.in_([f"user:{user_id}" for user_id in missing]))
                .group_by(AgentMemory.scope)
            )
            names.update({scope.split(":", 1)[1]: name for scope, name in rows if name})

    def label(scope: str) -> str:
        if scope == GLOBAL_SCOPE:
            return "everyone"
        kind, _, ident = scope.partition(":")
        if kind == "user":
            return f"@{names[ident]}" if ident in names else ident
        return f"room {ident}"

    return [MemoryScopeOut(scope=scope, count=count, label=label(scope)) for scope, count in scopes]


@router.post("/notes", response_model=MemoryNoteOut, status_code=status.HTTP_201_CREATED)
async def create_note(
    payload: MemoryNoteCreate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> MemoryNoteOut:
    snapshot = await runtime.config_store.current()
    try:
        note, created = await _memory(runtime).add(
            payload.scope,
            payload.text,
            embedding=snapshot.embedding,
            source="portal",
            created_by=admin.email,
        )
    except NoteError as exc:
        raise _unprocessable(str(exc)) from exc
    if not created:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Note #{note.id} already says this: {note.text}",
        )
    record_audit(session, admin, "create", "agent_memory", note.id, payload.scope)
    await session.commit()
    return _note_out(note)


@router.put("/notes/{note_id}", response_model=MemoryNoteOut)
async def update_note(
    note_id: int, payload: MemoryNoteUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> MemoryNoteOut:
    snapshot = await runtime.config_store.current()
    try:
        note = await _memory(runtime).update_text(note_id, payload.text, embedding=snapshot.embedding)
    except NoteError as exc:
        raise _unprocessable(str(exc)) from exc
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such note")
    record_audit(session, admin, "update", "agent_memory", note_id, note.scope)
    await session.commit()
    return _note_out(note)


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep) -> None:
    note = await _memory(runtime).delete(note_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such note")
    record_audit(session, admin, "delete", "agent_memory", note_id, note.scope)
    await session.commit()


@router.delete("/scopes/{scope}", status_code=status.HTTP_204_NO_CONTENT)
async def forget_scope(scope: str, admin: AdminUser, session: SessionDep, runtime: RuntimeDep) -> None:
    """Forget every note of one person, room, or of everyone."""
    removed = await _memory(runtime).forget_scope(scope)
    record_audit(session, admin, "delete", "agent_memory", scope, f"{removed} note(s)")
    await session.commit()


# --------------------------------------------------------------------------- #
# Profiles
# --------------------------------------------------------------------------- #


def _profile_out(row: AgentUserProfile, notes: int = 0) -> ProfileOut:
    return ProfileOut(
        rc_user_id=row.rc_user_id,
        username=row.username,
        display_name=row.display_name,
        language=row.language,
        timezone=row.timezone,
        about=row.about,
        notes=notes,
        updated_at=row.updated_at,
    )


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(_: AdminUser, session: SessionDep) -> list[ProfileOut]:
    rows = (await session.scalars(select(AgentUserProfile).order_by(AgentUserProfile.username))).all()
    counts = dict(
        (
            await session.execute(
                select(AgentMemory.scope, func.count())
                .where(AgentMemory.scope.like("user:%"))
                .group_by(AgentMemory.scope)
            )
        ).all()
    )
    return [_profile_out(row, counts.get(f"user:{row.rc_user_id}", 0)) for row in rows]


@router.put("/profiles/{rc_user_id}", response_model=ProfileOut)
async def update_profile(
    rc_user_id: str, payload: ProfileUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> ProfileOut:
    fields = payload.model_dump(exclude_unset=True)
    username = (fields.pop("username", None) or "").strip().lstrip("@")
    try:
        await _profiles(runtime).update(rc_user_id, username, **fields)
    except ProfileError as exc:
        raise _unprocessable(str(exc)) from exc
    record_audit(session, admin, "update", "agent_user_profile", rc_user_id, ", ".join(sorted(fields)))
    await session.commit()
    row = await session.scalar(
        select(AgentUserProfile).where(AgentUserProfile.rc_user_id == rc_user_id)
    )
    assert row is not None
    return _profile_out(row)


@router.delete("/profiles/{rc_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    rc_user_id: str, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    row = await session.scalar(
        select(AgentUserProfile).where(AgentUserProfile.rc_user_id == rc_user_id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such profile")
    await session.delete(row)
    record_audit(session, admin, "delete", "agent_user_profile", rc_user_id, row.username)
    await session.commit()
    _profiles(runtime).forget(rc_user_id)
