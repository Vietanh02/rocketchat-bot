"""The knowledge base: documents uploaded here are what the assistant looks
questions up in, unless the conversation sent documents of its own.

Documents sent in chat are listed too, so admins can see and remove them.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import zipfile
from pathlib import PurePosixPath

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select

from app.agent.intent import detect
from app.api.deps import AdminUser, CurrentUser, RuntimeDep, SessionDep, record_audit
from app.api.memory import _agent
from app.bot.config_store import bump_config_version, ensure_knowledge_config
from app.knowledge.base import BASE_SCOPE, DocumentError, DocumentInfo, KnowledgeBase, Passage
from app.knowledge.folder import (
    FolderImportError,
    ImportReport,
    from_directory,
    from_zip,
    import_root,
    server_folder,
    server_folders,
    valid_collection,
)
from app.knowledge.parsers import MAX_FILE_BYTES, SUPPORTED, is_supported
from app.knowledge.vectorstore import VectorStoreError
from app.models import KbDocument, KbPassage, KnowledgeConfig
from app.runtime import Runtime
from app.schemas import (
    FolderImportIn,
    ImportFailureOut,
    ImportReportOut,
    ImportsOut,
    KbCollectionOut,
    KbDocumentOut,
    KbPassageOut,
    KbSearchOut,
    KbStatusOut,
    KbUploadResult,
    KnowledgeConfigOut,
    KnowledgeConfigUpdate,
    ServerFolderOut,
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
        collection=info.collection,
        source_path=info.source_path,
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
    source: str | None = Query(default=None, pattern="^(portal|chat|folder)$"),
    collection: str | None = Query(default=None, max_length=60),
) -> list[KbDocumentOut]:
    """Uploaded on the portal (source=portal), sent in chat (chat), imported (folder), or all.

    `collection` narrows it to one imported folder.
    """
    snapshot = await runtime.config_store.current()
    documents = await _kb(runtime).documents(
        source=source, embedding=snapshot.embedding, collection=collection
    )
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


# --------------------------------------------------------------------------- #
# Whole folders (e.g. a Confluence export), as a ZIP or from the server
# --------------------------------------------------------------------------- #

# A ZIP uploaded for import; it is unpacked one file at a time.
MAX_ZIP_BYTES = 1024 * 1024 * 1024


def _report_out(report: ImportReport) -> ImportReportOut:
    return ImportReportOut(
        id=report.id,
        collection=report.collection,
        origin=report.origin,
        status=report.status,  # type: ignore[arg-type]
        total=report.total,
        done=report.done,
        added=report.added,
        updated=report.updated,
        unchanged=report.unchanged,
        removed=report.removed,
        duplicates=report.duplicates,
        skipped=report.skipped,
        failures=[ImportFailureOut(path=p, error=e) for p, e in report.failures],
        error=report.error,
        started_at=report.started_at,
        ended_at=report.ended_at,
    )


def _flag(value: str | None, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _collection(name: str | None, fallback: str) -> str:
    try:
        return valid_collection(name or re.sub(r"[^A-Za-z0-9 _.-]", "-", fallback).strip("-. ") or "import")
    except FolderImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


def _busy(runtime: Runtime, collection: str) -> None:
    for report in _agent(runtime).imports.reports.values():
        if report.status == "running" and report.collection == collection:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{collection} is being imported already; wait for it to finish.",
            )


@router.get("/imports", response_model=ImportsOut)
async def list_imports(_: CurrentUser, runtime: RuntimeDep) -> ImportsOut:
    agent = _agent(runtime)
    return ImportsOut(
        import_dir=str(import_root()),
        folders=[ServerFolderOut(**folder) for folder in server_folders()],
        collections=[
            KbCollectionOut(name=name, documents=documents, passages=passages)
            for name, documents, passages in await agent.kb.collections()
        ],
        imports=[_report_out(r) for r in sorted(agent.imports.reports.values(), key=lambda r: -r.id)],
    )


@router.get("/imports/{import_id}", response_model=ImportReportOut)
async def read_import(import_id: int, _: CurrentUser, runtime: RuntimeDep) -> ImportReportOut:
    report = _agent(runtime).imports.reports.get(import_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such import")
    return _report_out(report)


@router.post("/imports/zip", response_model=ImportReportOut, status_code=status.HTTP_202_ACCEPTED)
async def import_zip(
    admin: AdminUser,
    session: SessionDep,
    runtime: RuntimeDep,
    file: UploadFile = File(...),
    collection: str | None = Form(default=None),
    # Text, not bool: a bool form field left out reads as false, whatever its default.
    model_index: str | None = Form(default=None),
    prune: str | None = Form(default=None),
    attachments: str | None = Form(default=None),
) -> ImportReportOut:
    """Import every document of a ZIP (such as a zipped Confluence export) as one collection."""
    name = _collection(collection, PurePosixPath(file.filename or "import").stem)
    _busy(runtime, name)
    # The upload is gone once this returns; the import reads its own copy.
    spool = tempfile.NamedTemporaryFile(prefix="kb-import-", suffix=".zip", delete=False)
    try:
        size = 0
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=f"The ZIP is larger than {MAX_ZIP_BYTES // 1024**3} GB; copy the folder to the server instead.",
                )
            spool.write(chunk)
        spool.close()
        try:
            archive = zipfile.ZipFile(spool.name)
            files = from_zip(archive)
        except (zipfile.BadZipFile, FolderImportError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Cannot read the ZIP: {exc}",
            ) from exc
    except BaseException:
        spool.close()
        os.unlink(spool.name)
        raise

    def cleanup() -> None:
        archive.close()
        os.unlink(spool.name)

    snapshot = await runtime.config_store.current()
    report = _agent(runtime).imports.start(
        name,
        files,
        origin=f"upload: {file.filename or 'archive.zip'}",
        embedding=snapshot.embedding,
        model_index=_flag(model_index, True) and snapshot.knowledge.llm_index,
        prune=_flag(prune, True),
        attachments=_flag(attachments, False),
        uploaded_by=admin.email,
        cleanup=cleanup,
    )
    record_audit(session, admin, "create", "kb_import", report.id, f"{name} from {file.filename}")
    await session.commit()
    return _report_out(report)


@router.post("/imports/folder", response_model=ImportReportOut, status_code=status.HTTP_202_ACCEPTED)
async def import_folder(
    payload: FolderImportIn, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> ImportReportOut:
    """Import a folder copied to the server's import folder."""
    try:
        folder = server_folder(payload.folder)
        files = await asyncio.to_thread(from_directory, folder)
    except FolderImportError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    name = _collection(payload.collection, payload.folder)
    _busy(runtime, name)
    snapshot = await runtime.config_store.current()
    report = _agent(runtime).imports.start(
        name,
        files,
        origin=f"folder: {payload.folder}",
        embedding=snapshot.embedding,
        model_index=payload.model_index and snapshot.knowledge.llm_index,
        prune=payload.prune,
        attachments=payload.attachments,
        uploaded_by=admin.email,
    )
    record_audit(session, admin, "create", "kb_import", report.id, f"{name} from folder {payload.folder}")
    await session.commit()
    return _report_out(report)


@router.delete("/collections/{name}")
async def delete_collection(
    name: str, admin: AdminUser, session: SessionDep, runtime: RuntimeDep
) -> dict[str, int]:
    """Remove every document an import of `name` added."""
    _busy(runtime, name)
    removed = await _kb(runtime).forget_collection(name)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such collection")
    record_audit(session, admin, "delete", "kb_collection", None, f"{name}: {removed} documents")
    await session.commit()
    return {"removed": removed}
