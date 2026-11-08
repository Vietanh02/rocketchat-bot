"""Cutting documents into passages that can be found and quoted.

A passage is about 1000 characters (roughly 250 – 400 tokens), cut at
paragraph, then sentence, then word boundaries, and overlaps the previous
one a little so a sentence split between two is still found whole in one.
Each passage remembers the heading it falls under and the page it starts
on, for citations.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.knowledge.parsers import Page

TARGET_CHARS = 1000
MAX_CHARS = 1400
OVERLAP_CHARS = 150

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_SENTENCE_END = re.compile(r"(?<=[.!?…:;])\s+")


@dataclass(slots=True)
class Chunk:
    text: str
    heading: str = ""
    page: int | None = None


def _pieces(paragraph: str, limit: int) -> list[str]:
    """`paragraph` in parts of at most `limit` characters, at sentence or word ends."""
    if len(paragraph) <= limit:
        return [paragraph]
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(paragraph):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > limit // 2 else limit
            head, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if current:
                parts.append(current)
                current = ""
            parts.append(head)
        if current and len(current) + 1 + len(sentence) > limit:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return parts


def chunk_pages(
    pages: Iterable[Page],
    *,
    target: int = TARGET_CHARS,
    maximum: int = MAX_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading = ""
    # The passage being built: the tail carried over from the previous one,
    # then new pieces.
    carried = ""
    pieces: list[str] = []
    size = 0
    start_page: int | None = None
    start_heading = ""

    def flush(*, carry: bool = True) -> None:
        nonlocal carried, pieces, size
        if pieces:
            text = "\n\n".join([carried, *pieces] if carried else pieces).strip()
            chunks.append(Chunk(text, start_heading, start_page))
            tail = text[-overlap:] if carry and overlap and len(text) > overlap else ""
            space = tail.find(" ")
            carried = tail[space + 1 :] if 0 <= space < len(tail) - 1 else tail
        elif not carry:
            carried = ""
        pieces, size = [], len(carried)

    for page in pages:
        for paragraph in re.split(r"\n\s*\n", page.text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            match = _HEADING.match(paragraph.split("\n", 1)[0])
            if match:
                # A new section starts a new passage, without the old one's tail;
                # headings with nothing under them yet stay with the next section.
                if any(not _HEADING.match(piece) for piece in pieces):
                    flush(carry=False)
                carried = ""
                size = sum(len(piece) + 2 for piece in pieces)
                heading = match.group(2).strip()
            for piece in _pieces(paragraph, maximum):
                if pieces and size + len(piece) > target:
                    flush()
                if not pieces:
                    start_page = page.number
                start_heading = heading if not pieces or all(
                    _HEADING.match(p) for p in pieces
                ) else start_heading
                pieces.append(piece)
                size += len(piece) + 2
    flush()
    return chunks
