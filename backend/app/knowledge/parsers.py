"""Text out of uploaded documents.

PDF (pypdf, page by page so answers can cite a page), Word .docx
(python-docx, headings kept as Markdown headings), and plain text formats:
Markdown, text, CSV, JSON and HTML (tags stripped with the standard
library). Scanned PDFs have no text layer and come out empty; that is
reported rather than indexed as nothing.

Parsing is CPU work; callers run it in a thread (`asyncio.to_thread`).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import PurePath

# Bigger files are refused before they are read.
MAX_FILE_BYTES = 20 * 1024 * 1024
# Text kept from one document; the rest is dropped (with a note).
MAX_TEXT_CHARS = 2_000_000

TEXT_TYPES = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log", ".rst", ".yaml", ".yml"}
HTML_TYPES = {".html", ".htm"}
SUPPORTED = {".pdf", ".docx", *TEXT_TYPES, *HTML_TYPES}
# What Rocket.Chat or browsers say, for files without a usable extension.
MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
}


class ParseError(ValueError):
    """The file cannot be read; the message says why, for the person who sent it."""


@dataclass(slots=True)
class Page:
    text: str
    # 1-based; None for formats without pages.
    number: int | None = None


@dataclass(slots=True)
class ParsedDocument:
    title: str
    pages: list[Page] = field(default_factory=list)
    truncated: bool = False

    @property
    def chars(self) -> int:
        return sum(len(page.text) for page in self.pages)


def extension_for(filename: str, content_type: str | None = None) -> str:
    suffix = PurePath(filename or "").suffix.lower()
    if suffix in SUPPORTED:
        return suffix
    return MIME_EXTENSIONS.get((content_type or "").split(";")[0].strip().lower(), suffix)


def is_supported(filename: str, content_type: str | None = None) -> bool:
    return extension_for(filename, content_type) in SUPPORTED


def parse(filename: str, data: bytes, content_type: str | None = None) -> ParsedDocument:
    """The text of a document; raises ParseError."""
    if len(data) > MAX_FILE_BYTES:
        raise ParseError(f"the file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    kind = extension_for(filename, content_type)
    title = PurePath(filename or "document").stem or "document"
    if kind == ".pdf":
        pages = _pdf(data)
    elif kind == ".docx":
        pages = [Page(_docx(data))]
    elif kind in HTML_TYPES:
        pages = [Page(_html(_decode(data)))]
    elif kind in TEXT_TYPES:
        pages = [Page(_decode(data))]
    else:
        raise ParseError(
            f"{PurePath(filename).suffix or 'this file type'} is not supported; "
            "send PDF, Word (.docx), Markdown, text, CSV or HTML"
        )

    pages = [Page(_tidy(page.text), page.number) for page in pages]
    pages = [page for page in pages if page.text]
    document = ParsedDocument(title=title, pages=pages)
    if not pages:
        raise ParseError(
            "no text could be read from it"
            + (" (a scanned PDF has only images of text)" if kind == ".pdf" else "")
        )
    total = 0
    for index, page in enumerate(pages):
        total += len(page.text)
        if total > MAX_TEXT_CHARS:
            page.text = page.text[: max(0, len(page.text) - (total - MAX_TEXT_CHARS))]
            document.pages = [p for p in pages[: index + 1] if p.text]
            document.truncated = True
            break
    return document


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-16" and not data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            continue
        return text
    return data.decode("cp1258", errors="replace")


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _pdf(data: bytes) -> list[Page]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001 - pypdf raises several kinds
                raise ParseError("the PDF is password-protected") from exc
        pages = []
        for number, page in enumerate(reader.pages, start=1):
            try:
                pages.append(Page(page.extract_text() or "", number))
            except Exception:  # noqa: BLE001 - one broken page should not lose the rest
                pages.append(Page("", number))
        return pages
    except ParseError:
        raise
    except (PdfReadError, ValueError, KeyError, OSError) as exc:
        raise ParseError(f"the PDF could not be read ({exc})") from exc


def _docx(data: bytes) -> str:
    try:
        import docx
        from docx.opc.exceptions import PackageNotFoundError
    except ImportError as exc:  # pragma: no cover - a dependency
        raise ParseError("Word files cannot be read on this server") from exc
    try:
        document = docx.Document(io.BytesIO(data))
    except (PackageNotFoundError, ValueError, KeyError, OSError) as exc:
        raise ParseError(f"the Word file could not be read ({exc})") from exc

    lines: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            lines.append("")
            continue
        style = (paragraph.style.name if paragraph.style is not None else "") or ""
        match = re.match(r"Heading (\d)", style)
        if match:
            lines.append(f"{'#' * int(match.group(1))} {text}")
        elif style == "Title":
            lines.append(f"# {text}")
        else:
            lines.append(text)
    for table in document.tables:
        lines.append("")
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            lines.append(" | ".join(cells))
    return "\n".join(lines)


class _TextOnly(HTMLParser):
    SKIP = {"script", "style", "noscript", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "section", "article", "table", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP:
            self._skipping += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self.BLOCK or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _html(text: str) -> str:
    parser = _TextOnly()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)
