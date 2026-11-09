"""Importing a whole folder into the knowledge base, such as a Confluence export.

A folder arrives as a ZIP uploaded on the portal, or as a folder copied to
the server's import folder (`APP_KB_IMPORT_DIR`, `kb_import/` next to
docker-compose.yml). Every supported file in it becomes a document of the
knowledge base, in a *collection* named after the import, remembered by its
path in the folder. Importing the same folder again under the same name
syncs it: unchanged files are left alone, changed ones replaced, and files
gone from the folder removed.

Markdown from `confluence-markdown-exporter` (`cme spaces <url>`) is read
the way it is written:

- pages are `{space}/{homepage}/{ancestors}/{page}.md`, with the title as
  an H1 and breadcrumb links at the top; the title comes from the H1 (or
  the front matter, or the file name);
- links become their text and images are dropped, so search sees words,
  not paths; front matter is dropped except its `tags`;
- attachments are `{space}/attachments/<file id>.<ext>`, named by id: they
  are left out unless asked for, and then take the name the pages' links
  give them;
- `confluence-lock.json` and `*.comments.md` are skipped.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import posixpath
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import unquote

import yaml

from app.config import get_settings
from app.knowledge.base import BASE_SCOPE, DocumentError
from app.knowledge.parsers import MAX_FILE_BYTES, is_supported

if TYPE_CHECKING:
    from app.bot.config_store import EmbeddingSettings
    from app.knowledge.base import KnowledgeBase

log = logging.getLogger(__name__)

# A ZIP may hold this much once unpacked, and this many files.
MAX_UNPACKED_BYTES = 2 * 1024**3
MAX_FILES = 20_000
# Failures listed in a report.
MAX_LISTED_FAILURES = 100
ATTACHMENTS = "attachments"
SKIPPED_NAMES = {"confluence-lock.json"}
MARKDOWN = {".md", ".markdown"}
COLLECTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")

_FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|!\[\[[^\]]*\]\]")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_WIKI = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.M)


class FolderImportError(ValueError):
    """An import was refused; the message says why."""


@dataclass(slots=True)
class FolderFile:
    # Relative, with forward slashes.
    path: str
    size: int
    read: Callable[[], bytes]


def valid_collection(name: str) -> str:
    name = " ".join(name.split())
    if not COLLECTION_NAME.match(name):
        raise FolderImportError(
            "a collection name is 1 to 60 letters, digits, spaces, '-', '_' or '.', "
            "starting with a letter or digit"
        )
    return name


def _hidden(path: PurePosixPath) -> bool:
    return any(part.startswith((".", "__MACOSX")) for part in path.parts)


def from_directory(root: Path) -> list[FolderFile]:
    root = root.resolve()
    files: list[FolderFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if _hidden(relative):
            continue
        files.append(FolderFile(str(relative), path.stat().st_size, path.read_bytes))
        if len(files) > MAX_FILES:
            raise FolderImportError(f"the folder holds more than {MAX_FILES} files")
    return files


def from_zip(archive: zipfile.ZipFile) -> list[FolderFile]:
    """The files of a ZIP, with one folder holding everything taken off their paths."""
    members = [m for m in archive.infolist() if not m.is_dir()]
    if len(members) > MAX_FILES:
        raise FolderImportError(f"the ZIP holds more than {MAX_FILES} files")
    if sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
        raise FolderImportError(f"the ZIP unpacks to more than {MAX_UNPACKED_BYTES // 1024**3} GB")
    paths = []
    for member in members:
        # Never written to disk, but a path must still stay inside the folder.
        path = PurePosixPath(posixpath.normpath(member.filename.replace("\\", "/")))
        if path.is_absolute() or ".." in path.parts or _hidden(path):
            continue
        paths.append((path, member))
    tops = {p.parts[0] for p, _ in paths if len(p.parts) > 1}
    strip = len(tops) == 1 and all(len(p.parts) > 1 for p, _ in paths)
    files = []
    for path, member in sorted(paths, key=lambda item: str(item[0])):
        relative = PurePosixPath(*path.parts[1:]) if strip else path
        files.append(
            FolderFile(str(relative), member.file_size, lambda m=member: archive.read(m))
        )
    return files


def is_attachment(path: str) -> bool:
    return ATTACHMENTS in PurePosixPath(path).parts[:-1]


def wanted(path: str, attachments: bool) -> bool:
    name = PurePosixPath(path).name
    if name in SKIPPED_NAMES or name.endswith(".comments.md"):
        return False
    if is_attachment(path) and not attachments:
        return False
    return is_supported(name)


def clean_markdown(text: str) -> tuple[str | None, str]:
    """(title, text) of a Markdown page, with links as their words and no images."""
    title = None
    match = _FRONT.match(text)
    if match is not None:
        try:
            front = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            front = {}
        text = text[match.end() :]
        if isinstance(front, dict):
            if front.get("title"):
                title = str(front["title"]).strip()
            tags = front.get("tags")
            if isinstance(tags, list) and tags:
                text = "Tags: " + ", ".join(str(t) for t in tags) + "\n\n" + text
    text = _IMAGE.sub("", text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _WIKI.sub(lambda m: m.group(2) or m.group(1), text)
    if title is None:
        heading = _H1.search(text)
        if heading is not None:
            title = heading.group(1).strip()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return title, text


def attachment_titles(pages: dict[str, str]) -> dict[str, str]:
    """What the pages' links call each attachment: {its path: the link's text}."""
    titles: dict[str, str] = {}
    for page, text in pages.items():
        folder = posixpath.dirname(page)
        for label, target in _LINK.findall(text):
            if not label.strip() or "://" in target or target.startswith("#"):
                continue
            path = posixpath.normpath(posixpath.join(folder, unquote(target.split("#", 1)[0])))
            if is_attachment(path):
                titles.setdefault(path, label.strip())
    return titles


@dataclass(slots=True)
class ImportReport:
    id: int
    collection: str
    origin: str
    status: str = "running"  # running|done|failed
    total: int = 0
    done: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    duplicates: int = 0
    skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    def fail(self, path: str, why: str) -> None:
        if len(self.failures) < MAX_LISTED_FAILURES:
            self.failures.append((path, why))


def import_root() -> Path:
    return Path(get_settings().kb_import_dir).expanduser().resolve()


def server_folders() -> list[dict]:
    """The folders waiting in the import folder, with how many files each holds."""
    root = import_root()
    if not root.is_dir():
        return []
    found = []
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and not folder.name.startswith("."):
            count = sum(1 for p in folder.rglob("*") if p.is_file())
            found.append({"name": folder.name, "files": count})
    return found


def server_folder(name: str) -> Path:
    root = import_root()
    folder = (root / name).resolve()
    if not name or "/" in name or not folder.is_relative_to(root) or not folder.is_dir():
        raise FolderImportError(f"there is no folder {name!r} in {root}")
    return folder


class FolderImporter:
    """Runs imports one at a time, in the background, and keeps their reports."""

    def __init__(self, kb: KnowledgeBase) -> None:
        self._kb = kb
        self.reports: dict[int, ImportReport] = {}
        self._next = 1
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def start(
        self,
        collection: str,
        files: Iterable[FolderFile],
        *,
        origin: str,
        embedding: EmbeddingSettings,
        model_index: bool = True,
        prune: bool = True,
        attachments: bool = False,
        uploaded_by: str | None = None,
        cleanup: Callable[[], None] | None = None,
    ) -> ImportReport:
        report = ImportReport(self._next, collection, origin)
        self._next += 1
        self.reports[report.id] = report
        # The latest twenty are kept.
        for old in sorted(self.reports)[:-20]:
            self.reports.pop(old, None)
        task = asyncio.create_task(
            self._run(report, list(files), embedding, model_index, prune, attachments, uploaded_by, cleanup),
            name=f"kb-import-{report.id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return report

    async def _run(
        self,
        report: ImportReport,
        files: list[FolderFile],
        embedding: EmbeddingSettings,
        model_index: bool,
        prune: bool,
        attachments: bool,
        uploaded_by: str | None,
        cleanup: Callable[[], None] | None,
    ) -> None:
        try:
            async with self._lock:
                await self._import(report, files, embedding, model_index, prune, attachments, uploaded_by)
            report.status = "done"
        except asyncio.CancelledError:
            report.status, report.error = "failed", "stopped"
            raise
        except Exception as exc:  # noqa: BLE001 - reported on the portal
            log.exception("Import %d (%s) failed", report.id, report.collection)
            report.status, report.error = "failed", str(exc)[:500] or exc.__class__.__name__
        finally:
            report.ended_at = datetime.now(timezone.utc)
            if cleanup is not None:
                with contextlib.suppress(Exception):
                    cleanup()
            log.info(
                "Import %d of %s: %d added, %d updated, %d unchanged, %d removed, %d failed",
                report.id, report.collection, report.added, report.updated,
                report.unchanged, report.removed, len(report.failures),
            )

    async def _import(
        self,
        report: ImportReport,
        files: list[FolderFile],
        embedding: EmbeddingSettings,
        model_index: bool,
        prune: bool,
        attachments: bool,
        uploaded_by: str | None,
    ) -> None:
        chosen = [f for f in files if wanted(f.path, attachments)]
        report.skipped = len(files) - len(chosen)
        report.total = len(chosen)
        known = await self._kb.collection_files(report.collection)

        # Pages first: their links name the attachments.
        texts: dict[str, str] = {}
        for item in chosen:
            if PurePosixPath(item.path).suffix.lower() in MARKDOWN and item.size <= MAX_FILE_BYTES:
                texts[item.path] = (await asyncio.to_thread(item.read)).decode("utf-8", errors="replace")
        names = attachment_titles(texts) if attachments else {}

        for item in chosen:
            await self._one(report, item, texts, names, known, embedding, model_index, uploaded_by)
            report.done += 1
        if prune:
            for path, (document_id, _) in known.items():
                await self._kb.delete(document_id, [BASE_SCOPE])
                report.removed += 1

    async def _one(
        self,
        report: ImportReport,
        item: FolderFile,
        texts: dict[str, str],
        names: dict[str, str],
        known: dict[str, tuple[int, str]],
        embedding: EmbeddingSettings,
        model_index: bool,
        uploaded_by: str | None,
    ) -> None:
        if item.size > MAX_FILE_BYTES:
            known.pop(item.path, None)  # kept as it was
            report.fail(item.path, f"larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
            return
        path = PurePosixPath(item.path)
        named = names.get(item.path)
        # "Policy scan.pdf" in a link: the title is "Policy scan".
        title = PurePosixPath(named).stem if named and PurePosixPath(named).suffix.lower() == path.suffix.lower() else named
        if item.path in texts:
            heading, body = clean_markdown(texts[item.path])
            title = heading or path.stem
            data = body.encode("utf-8")
        else:
            data = await asyncio.to_thread(item.read)
        digest = hashlib.sha256(data).hexdigest()
        old = known.pop(item.path, None)
        if old is not None and old[1] == digest:
            report.unchanged += 1
            return
        if old is not None:
            await self._kb.delete(old[0], [BASE_SCOPE])
        filename = f"{title}{path.suffix}" if named else path.name
        try:
            info, created = await self._kb.add(
                BASE_SCOPE,
                filename,
                data,
                embedding=embedding,
                source="folder",
                uploaded_by=uploaded_by,
                title=title or path.stem,
                collection=report.collection,
                source_path=item.path,
                model_index=model_index,
            )
        except DocumentError as exc:
            report.fail(item.path, str(exc))
            return
        if not created:
            # The same text is already in the knowledge base (an empty page, a copy).
            report.duplicates += 1
            return
        if old is not None:
            report.updated += 1
        else:
            report.added += 1

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
