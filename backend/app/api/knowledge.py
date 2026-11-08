"""The knowledge base: documents uploaded here are what the assistant looks
questions up in, unless the conversation sent documents of its own.

Documents sent in chat are listed too, so admins can see and remove them.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select

from app.agent.intent import detect
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.api.memory import _agent
from app.bot.config_store import bump_config_version, ensure_knowledge_config
from app.knowledge.base import BASE_SCOPE, DocumentError, DocumentInfo, KnowledgeBase, Passage
from app.knowledge.parsers import MAX_FILE_BYTES, SUPPORTED, is_supported
from app.knowledge.vectorstore import VectorStoreError
from app.models import KbDocument, KbPassage, KnowledgeConfig
from app.runtime import Runtime
from app.schemas import (
    KbDocumentOut,
    KbPassageOut,
    KbSearchOut,
    KbStatusOut,
    KbUploadResult,
    KnowledgeConfigOut,
    KnowledgeConfigUpdate,
)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])

# Files accepted in one upload.
MAX_FILES_PER_UPLOAD = 20


def _kb(runtime: Runtime) -> KnowledgeBase:
    return _agent(runtime).kb


def _config_out(config: KnowledgeConfig) -> KnowledgeConfigOut:
    return KnowledgeConfigOut(
        enabled=config.enabled,
        chat_uploads=config.chat_uploads,
        top_k=config.top_k,
        min_score=config.min_score,
        intent_mode=config.intent_mode,
        llm_index=config.llm_index,
        llm_judge=config.llm_judge,
        index_model=config.index_model,
    )


def _document_out(info: DocumentInfo) -> KbDocumentOut:
    return KbDocumentOut(
        id=info.id,
        scope=info.scope,
        title=info.title,
        filename=info.filename,
        content_type=info.content_type,
        size_bytes=info.size_bytes,
        pages=info.pages,
        chunks=info.chunks,
        chars=info.chars,
        truncated=info.truncated,
        source=info.source,
        uploaded_by=info.uploaded_by,
        created_at=info.created_at,
        stage=info.stage,
        stage_error=info.stage_error,
        sections=info.sections,
        pending=info.pending,
    )


def _passage_out(passage: Passage) -> KbPassageOut:
    return KbPassageOut(
        id=passage.id,
        document_id=passage.document_id,
        scope=passage.scope,
        label=passage.label,
        text=passage.shown,
        score=None if passage.score is None else round(passage.score, 4),
    )


@router.get("/config", response_model=KnowledgeConfigOut)
async def read_knowledge_config(_: CurrentUser, session: SessionDep) -> KnowledgeConfigOut:
    config = await ensure_knowledge_config(session)
    await session.commit()
    return _config_out(config)


@router.put("/config", response_model=KnowledgeConfigOut)
async def update_knowledge_config(
    payload: KnowledgeConfigUpdate, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> KnowledgeConfigOut:
    config = await ensure_knowledge_config(session)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    turned_on = changes.get("llm_index") and not config.llm_index
    for field, value in changes.items():
        setattr(config, field, value)
    await bump_config_version(session)
    record_audit(
        session, admin, "update", "knowledge_config", config.id, ", ".join(sorted(changes)) or "(no changes)"
    )
    await session.commit()
    await session.refresh(config)
    await runtime.apply_config_change()
    if turned_on:
        # Documents cut by their headings only get the model's indexing now.
        kb = _kb(runtime)
        for info in await kb.documents():
            if info.stage in ("basic", "failed"):
                await kb.reprocess(info.id)
    return _config_out(config)


@router.get("/status", response_model=KbStatusOut)
async def knowledge_status(_: CurrentUser, session: SessionDep, runtime: RuntimeDep) -> KbStatusOut:
    snapshot = await runtime.config_store.current()
    kb = _kb(runtime)
    base = await session.scalar(
        select(func.count()).select_from(KbDocument).where(KbDocument.scope == BASE_SCOPE)
    )
    chat = await session.scalar(
        select(func.count()).select_from(KbDocument).where(KbDocument.scope != BASE_SCOPE)
    )
    passages = await session.scalar(select(func.count()).select_from(KbPassage)) or 0
    semantic = snapshot.embedding.configured and kb.knowledge.store is not None
    if semantic:
        try:
            await kb.knowledge.store.ping()  # type: ignore[union-attr]
        except VectorStoreError:
            semantic = False
    return KbStatusOut(
        documents=base or 0,
        chat_documents=chat or 0,
        passages=passages,
        pending=await kb.pending_count(snapshot.embedding) if snapshot.embedding.configured else passages,
        semantic=semantic,
        stages=await kb.stage_counts(),
    )


@router.get("/documents", response_model=list[KbDocumentOut])
async def list_documents(
    _: CurrentUser,
    runtime: RuntimeDep,
    source: str | None = Query(default=None, pattern="^(portal|chat)$"),
) -> list[KbDocumentOut]:
    """The knowledge base (source=portal), documents sent in chat (source=chat), or both."""
    snapshot = await runtime.config_store.current()
    documents = await _kb(runtime).documents(source=source, embedding=snapshot.embedding)
    return [_document_out(info) for info in documents]


@router.post("/documents", response_model=list[KbUploadResult])
async def upload_documents(
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
    files: list[UploadFile] = File(...),
) -> list[KbUploadResult]:
    """Add files to the knowledge base; each is reported on its own."""
    if len(files) > MAX_FILES_PER_UPLOAD:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Upload at most {MAX_FILES_PER_UPLOAD} files at a time.",
        )
    snapshot = await runtime.config_store.current()
    kb = _kb(runtime)
    results: list[KbUploadResult] = []
    for upload in files:
        name = upload.filename or "document"
        if not is_supported(name, upload.content_type):
            results.append(
                KbUploadResult(
                    filename=name,
                    ok=False,
                    message="Not a supported type: " + ", ".join(sorted(SUPPORTED)),
                )
            )
            continue
        data = await upload.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            results.append(
                KbUploadResult(
                    filename=name,
                    ok=False,
                    message=f"Larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.",
                )
            )
            continue
        try:
            info, created = await kb.add(
                BASE_SCOPE,
                name,
                data,
                content_type=upload.content_type,
                embedding=snapshot.embedding,
                source="portal",
                uploaded_by=admin.email,
            )
        except DocumentError as exc:
            results.append(KbUploadResult(filename=name, ok=False, message=str(exc).capitalize()))
            continue
        if not created:
            results.append(
                KbUploadResult(
                    filename=name,
                    ok=False,
                    message=f"Already in the knowledge base as “{info.title}”.",
                    document=_document_out(info),
                )
            )
            continue
        record_audit(session, admin, "create", "kb_document", info.id, name)
        results.append(
            KbUploadResult(
                filename=name,
                ok=True,
                message=f"{info.chunks} passages"
                + (f" from {info.pages} pages" if info.pages else "")
                + (" (cut short: the text is very long)" if info.truncated else ""),
                document=_document_out(info),
            )
        )
    await session.commit()
    return results


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> None:
    gone = await _kb(runtime).delete(document_id)
    if gone is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document")
    record_audit(session, admin, "delete", "kb_document", document_id, gone.filename)
    await session.commit()


@router.post("/documents/{document_id}/reprocess", response_model=KbDocumentOut)
async def reprocess_document(
    document_id: int, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> KbDocumentOut:
    """Have the language model index the document again."""
    kb = _kb(runtime)
    if not await kb.reprocess(document_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such document")
    record_audit(session, admin, "reprocess", "kb_document", document_id, "")
    await session.commit()
    snapshot = await runtime.config_store.current()
    [info] = [d for d in await kb.documents(embedding=snapshot.embedding) if d.id == document_id]
    return _document_out(info)


@router.post("/reindex", response_model=KbStatusOut)
async def reindex_knowledge(admin: AdminUser, session: SessionDep, runtime: RuntimeDep) -> KbStatusOut:
    snapshot = await runtime.config_store.current()
    if not snapshot.embedding.configured or _kb(runtime).knowledge.store is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Set up an embedding model and the vector database first (Memory page).",
        )
    await _kb(runtime).index_pending(snapshot.embedding)
    return await knowledge_status(admin, session, runtime)


@router.get("/search", response_model=KbSearchOut)
async def search_knowledge(
    _: CurrentUser,
    runtime: RuntimeDep,
    q: str = Query(min_length=1, max_length=1000),
) -> KbSearchOut:
    """What the assistant would do with `q` in a conversation without documents."""
    snapshot = await runtime.config_store.current()
    agent = _agent(runtime)
    llm = getattr(runtime.bot.responder, "llm", None)
    chat = llm.chat_for(snapshot.llm) if llm is not None and snapshot.llm.configured else None
    knowledge = snapshot.knowledge
    intent = await detect(
        q, mode=knowledge.intent_mode, chat=chat, fast_model=snapshot.agent.fast_model
    )
    judge_with = None
    if knowledge.llm_judge and chat is not None:
        judge_with = (chat, knowledge.index_model or snapshot.agent.fast_model or None)
    lookup = await agent.kb.lookup(
        q,
        "conv:portal-search",
        embedding=snapshot.embedding,
        limit=knowledge.top_k,
        min_score=knowledge.min_score,
        judge_with=judge_with,
    )
    return KbSearchOut(
        intent=intent.kind,
        intent_by=intent.by,
        judged=lookup.judged,
        source=lookup.source,
        passages=[_passage_out(p) for p in lookup.passages],
    )
