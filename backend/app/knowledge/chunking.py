"""Cutting a document into passages without changing a character.

The document's text is kept whole; everything here works with positions
in it. A passage is always `text[start:end]`, so what the model reads and
cites is exactly what the document says.

1. **Blocks** (`split_blocks`): the text's own units, found by its layout:
   headings, paragraphs and lists (up to a blank line), tables (consecutive
   `| … |` lines, or every row of a CSV/TSV file) and fenced code. A block
   longer than a passage may be is cut between lines, or for a single huge
   line between sentences, and never inside a table row. The pieces of a
   long table remember its header, which goes with them as context
   (shown as such, never mixed into the text).
2. **Sections**: consecutive blocks about one topic. The language model
   decides them (`app.knowledge.indexer`); without it, each heading starts
   one (`heading_sections`).
3. **Passages** (`pack`): the blocks of one section, packed in order up to
   `MAX_PASSAGE_CHARS`. A passage never spans two sections, and a heading
   always stays with what follows it.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.knowledge.parsers import Page

# About 300 – 450 tokens: small enough for embedding models that read 512.
MAX_PASSAGE_CHARS = 1200
PAGE_SEPARATOR = "\n\n"

_HEADING = re.compile(r"^(#{1,6})[ \t]+\S")
_TABLE_ROW = re.compile(r"^[ \t]*\|")
_TABLE_RULE = re.compile(r"^[ \t]*\|?[ \t]*:?-{2,}")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")
_SENTENCE_END = re.compile(r"(?<=[.!?…;:])\s+")


@dataclass(slots=True)
class Block:
    start: int
    end: int
    kind: str  # heading|paragraph|table|code
    page: int | None = None
    # Heading level (1–6) for headings.
    level: int = 0
    # The header rows of the table this piece continues, if any.
    header: str = ""


@dataclass(slots=True)
class Section:
    # Block indices, inclusive.
    first: int
    last: int
    title: str = ""
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Span:
    start: int
    end: int
    first_block: int
    last_block: int
    section: int
    page: int | None = None
    # Shown before the text, marked as context (a table's header).
    context: str = ""


def assemble(pages: Sequence[Page]) -> tuple[str, list[tuple[int, int | None]]]:
    """The whole text, and where each page starts in it: [(offset, page number)]."""
    parts: list[str] = []
    starts: list[tuple[int, int | None]] = []
    offset = 0
    for page in pages:
        if parts:
            parts.append(PAGE_SEPARATOR)
            offset += len(PAGE_SEPARATOR)
        starts.append((offset, page.number))
        parts.append(page.text)
        offset += len(page.text)
    return "".join(parts), starts


def _page_at(offset: int, starts: list[tuple[int, int | None]]) -> int | None:
    if not starts:
        return None
    index = bisect.bisect_right([s for s, _ in starts], offset) - 1
    return starts[max(0, index)][1]


def _lines(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """(start, end) of each line in text[start:end], without the newline."""
    spans = []
    position = start
    while position < end:
        newline = text.find("\n", position, end)
        stop = end if newline < 0 else newline
        spans.append((position, stop))
        position = stop + 1
    return spans


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """The span without surrounding blank lines (inner text untouched)."""
    while start < end and text[start] == "\n":
        start += 1
    while end > start and text[end - 1] in "\n \t":
        end -= 1
    return start, end


def split_blocks(
    text: str,
    page_starts: list[tuple[int, int | None]] | None = None,
    *,
    tabular: bool = False,
    limit: int = MAX_PASSAGE_CHARS,
) -> list[Block]:
    """The blocks of `text`, each at most `limit` characters.

    `tabular` reads every line as a table row, with the first as the
    header (CSV and TSV files).
    """
    page_starts = page_starts or []
    raw: list[Block] = []
    if tabular:
        start, end = _trim(text, 0, len(text))
        if start < end:
            first_line_end = text.find("\n", start, end)
            header = text[start : first_line_end if first_line_end >= 0 else end]
            raw.append(Block(start, end, "table", header=header))
    else:
        current: list[tuple[int, int]] = []
        kind = "paragraph"
        in_fence = False

        def close() -> None:
            nonlocal current
            if current:
                start, end = _trim(text, current[0][0], current[-1][1])
                if start < end:
                    raw.append(Block(start, end, kind))
            current = []

        for line_start, line_end in _lines(text, 0, len(text)):
            line = text[line_start:line_end]
            if in_fence:
                current.append((line_start, line_end))
                if _FENCE.match(line):
                    close()
                    in_fence = False
                continue
            if _FENCE.match(line):
                close()
                kind, in_fence = "code", True
                current.append((line_start, line_end))
                continue
            if not line.strip():
                close()
                continue
            heading = _HEADING.match(line)
            if heading:
                close()
                start, end = _trim(text, line_start, line_end)
                raw.append(Block(start, end, "heading", level=len(heading.group(1))))
                continue
            row = bool(_TABLE_ROW.match(line))
            if current and (kind == "table") != row:
                close()
            if not current:
                kind = "table" if row else "paragraph"
            current.append((line_start, line_end))
        close()

    blocks: list[Block] = []
    for block in raw:
        if block.kind == "table" and not block.header:
            rows = _lines(text, block.start, block.end)
            two = len(rows) > 1 and _TABLE_RULE.match(text[rows[1][0] : rows[1][1]])
            header_rows = rows[:2] if two else rows[:1]
            block.header = text[header_rows[0][0] : header_rows[-1][1]]
        for piece in _split_long(text, block, limit):
            piece.page = _page_at(piece.start, page_starts)
            blocks.append(piece)
    return blocks


def _split_long(text: str, block: Block, limit: int) -> list[Block]:
    if block.end - block.start <= limit:
        if block.kind == "table":
            # A table that fits needs no header beside it: it is in the text.
            block.header = ""
        return [block]
    pieces: list[Block] = []
    lines = _lines(text, block.start, block.end)
    group_start: int | None = None
    group_end = block.start

    def emit(start: int, end: int) -> None:
        if start < end:
            # Pieces after the first carry the table's header as context.
            header = block.header if block.kind == "table" and start > block.start else ""
            pieces.append(Block(start, end, block.kind, level=block.level, header=header))

    for line_start, line_end in lines:
        if line_end - line_start > limit:
            if group_start is not None:
                emit(group_start, group_end)
                group_start = None
            for start, end in _split_line(text, line_start, line_end, limit):
                emit(start, end)
            continue
        if group_start is not None and line_end - group_start > limit:
            emit(group_start, group_end)
            group_start = None
        if group_start is None:
            group_start = line_start
        group_end = line_end
    if group_start is not None:
        emit(group_start, group_end)
    return pieces


def _split_line(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """A line too long for a passage, cut after sentences, else between words."""
    spans: list[tuple[int, int]] = []
    cut_points = [m.start() for m in _SENTENCE_END.finditer(text, start, end)]
    piece_start = start
    while end - piece_start > limit:
        window_end = piece_start + limit
        candidates = [c for c in cut_points if piece_start + limit // 3 < c <= window_end]
        if candidates:
            cut = candidates[-1]
        else:
            space = text.rfind(" ", piece_start + limit // 3, window_end)
            cut = space if space > piece_start else window_end
        spans.append((piece_start, cut))
        piece_start = cut
        while piece_start < end and text[piece_start] in " \t":
            piece_start += 1
    if piece_start < end:
        spans.append((piece_start, end))
    return spans


def heading_sections(text: str, blocks: Sequence[Block]) -> list[Section]:
    """Sections from the headings alone: each heading starts one, titled by its path."""
    if not blocks:
        return []
    sections: list[Section] = []
    path: list[tuple[int, str]] = []
    first = 0
    title = ""
    for index, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        # Headings with nothing under them yet stay with the next one.
        has_body = any(b.kind != "heading" for b in blocks[first:index])
        if index > first and has_body:
            sections.append(Section(first, index - 1, title))
            first = index
        name = text[block.start : block.end].lstrip("#").strip()
        path = [(level, n) for level, n in path if level < block.level] + [(block.level, name)]
        title = " › ".join(n for _, n in path)
    sections.append(Section(first, len(blocks) - 1, title))
    return sections


def pack(
    text: str, blocks: Sequence[Block], sections: Sequence[Section], *, limit: int = MAX_PASSAGE_CHARS
) -> list[Span]:
    """Passages: each section's blocks packed in order, up to `limit` characters."""
    spans: list[Span] = []
    for number, section in enumerate(sections):
        first: int | None = None
        for index in range(section.first, section.last + 1):
            block = blocks[index]
            if first is not None:
                only_headings = all(blocks[i].kind == "heading" for i in range(first, index))
                # A table piece carrying a header starts its own passage, so the
                # header shown with it belongs to all of it.
                if not only_headings and (block.end - blocks[first].start > limit or block.header):
                    spans.append(_span(blocks, first, index - 1, number))
                    first = None
            if first is None:
                first = index
        if first is not None:
            spans.append(_span(blocks, first, section.last, number))
    return spans


def _span(blocks: Sequence[Block], first: int, last: int, section: int) -> Span:
    return Span(
        blocks[first].start,
        blocks[last].end,
        first,
        last,
        section,
        blocks[first].page,
        context=blocks[first].header,
    )


def chunk_pages(
    pages: Sequence[Page], *, tabular: bool = False
) -> tuple[str, list[Block], list[Section], list[Span]]:
    """The text, its blocks, heading-based sections and their passages, in one go."""
    text, starts = assemble(pages)
    blocks = split_blocks(text, starts, tabular=tabular)
    sections = heading_sections(text, blocks)
    return text, blocks, sections, pack(text, blocks, sections)
