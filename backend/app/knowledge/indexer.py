"""The language model's part in the knowledge base: indexing, and judging results.

**Indexing.** The model reads a document as numbered blocks (see
`app.knowledge.chunking`) and says which consecutive blocks form a section,
with a title, a one-sentence summary, keywords (with synonyms and other ways
to say them) and the questions the section answers. It never writes the
text: passages are cut from the document by block numbers, so whatever the
model answers, the content stays the document's own. What it writes goes
into the search index only, where it lets "lương ngoài giờ" find a section
titled "Làm thêm giờ".

Its answer is checked and repaired: sections are put in order, gaps and
overlaps are closed, and anything outside the blocks it was shown is
dropped. If nothing usable comes back, that part falls back to the
headings.

Long documents are sent in windows of about WINDOW_CHARS, cut before a
heading where there is one.

**Judging.** Given a question and the passages search found, the model
picks those that help answer it, best first. It reads more carefully than
any score, and can say that none do, so an unrelated passage is not handed
to the answer as if it were relevant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.knowledge.chunking import Block, Section, heading_sections

if TYPE_CHECKING:
    from app.bot.llm import LLMChat

log = logging.getLogger(__name__)

# How much of the document (as block previews) one request shows.
WINDOW_CHARS = 12_000
WINDOW_BLOCKS = 120
# Each block is shown up to this long; the model needs the gist, not all of it.
PREVIEW_CHARS = 400
INDEX_TIMEOUT_SECONDS = 180.0
INDEX_MAX_TOKENS = 6000
JUDGE_TIMEOUT_SECONDS = 8.0
JUDGE_PREVIEW_CHARS = 700
MAX_KEYWORDS = 20
MAX_QUESTIONS = 6

INDEX_PROMPT = """You index a document so that people can search it.
You get its blocks, numbered in order; long blocks are shortened with "…".
Group consecutive blocks into sections, each about one topic: usually
300 to 3000 characters, and a heading starts a new section.
Never rewrite, merge or correct the text: you only say where sections
start and end, and describe them.

For each section give:
- "first", "last": its first and last block numbers;
- "title": the section's own heading if it has one, else a short name;
- "summary": one sentence saying what it covers;
- "keywords": 8 to 15 search terms: the important words and names, their
  synonyms, abbreviations and other common ways people say them (e.g. for
  "làm thêm giờ": "tăng ca", "OT", "overtime", "lương ngoài giờ");
- "questions": 3 to 5 questions a reader would ask that this section
  answers, worded the way users ask.
Write in the language of the document; add English for technical terms.

Cover blocks {first} to {last} exactly once, in order.
Reply with JSON only:
{{"sections": [{{"first": 1, "last": 4, "title": "...", "summary": "...", "keywords": ["..."], "questions": ["..."]}}]}}"""

JUDGE_PROMPT = """You check search results for a question.
Pick the passages that contain information needed to answer it, the most
useful first. Leave out passages that are merely on a similar subject.
Reply with JSON only: {"passages": [3, 1]} (their numbers), or
{"passages": []} if none helps."""


class IndexingError(RuntimeError):
    """The model could not index the document."""


def _json_object(raw: str) -> dict | None:
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def _strings(value: object, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item, limit)
        if text and text.casefold() not in {o.casefold() for o in out}:
            out.append(text)
        if len(out) >= count:
            break
    return out


def windows(text: str, blocks: Sequence[Block]) -> list[tuple[int, int]]:
    """Ranges of block indices (inclusive) to index one request at a time."""
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(blocks):
        size, end = 0, start
        while end < len(blocks) and end - start < WINDOW_BLOCKS:
            size += min(PREVIEW_CHARS, blocks[end].end - blocks[end].start) + 12
            if size > WINDOW_CHARS and end > start:
                break
            end += 1
        if end < len(blocks):
            # Cut before a heading near the end, so a section is not split.
            for cut in range(end - 1, max(start, end - 25), -1):
                if blocks[cut].kind == "heading":
                    end = cut
                    break
        ranges.append((start, end - 1))
        start = end
    return ranges


def preview(text: str, blocks: Sequence[Block], first: int, last: int) -> str:
    lines = []
    for index in range(first, last + 1):
        block = blocks[index]
        body = text[block.start : block.end]
        if len(body) > PREVIEW_CHARS:
            body = body[:PREVIEW_CHARS].rstrip() + " …"
        where = f", page {block.page}" if block.page else ""
        lines.append(f"[{index + 1}] ({block.kind}{where})\n{body}")
    return "\n\n".join(lines)


def repair(raw_sections: object, first: int, last: int) -> list[Section]:
    """The model's sections over blocks first..last (0-based), made valid.

    The model numbers blocks from 1. Sections are sorted, clipped to the
    window, and made to follow one another with no gap or overlap; ones
    left empty are dropped.
    """
    if not isinstance(raw_sections, list):
        return []
    parsed: list[Section] = []
    for item in raw_sections:
        if not isinstance(item, dict):
            continue
        try:
            start, end = int(item["first"]) - 1, int(item["last"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        start, end = max(first, start), min(last, end)
        if start > end:
            continue
        parsed.append(
            Section(
                start,
                end,
                title=_clean(item.get("title", ""), 200),
                summary=_clean(item.get("summary", ""), 500),
                keywords=_strings(item.get("keywords"), MAX_KEYWORDS, 80),
                questions=_strings(item.get("questions"), MAX_QUESTIONS, 300),
            )
        )
    parsed.sort(key=lambda s: (s.first, s.last))
    sections: list[Section] = []
    for section in parsed:
        if sections:
            previous = sections[-1]
            if section.last <= previous.last:
                continue  # inside the previous one
            if section.first > previous.last + 1:
                # A gap: the blocks between go with the previous section.
                previous.last = section.first - 1
            section.first = previous.last + 1
        sections.append(section)
    if not sections:
        return []
    sections[0].first = first
    sections[-1].last = last
    return sections


async def index_sections(
    chat: LLMChat,
    text: str,
    blocks: Sequence[Block],
    *,
    title: str,
    model: str | None = None,
) -> tuple[list[Section], int]:
    """Sections of the whole document, and how many windows fell back to headings."""
    if not blocks:
        return [], 0
    sections: list[Section] = []
    fallbacks = 0
    for first, last in windows(text, blocks):
        found: list[Section] = []
        try:
            async with asyncio.timeout(INDEX_TIMEOUT_SECONDS):
                raw = await chat.complete(
                    [
                        {"role": "system", "content": INDEX_PROMPT.format(first=first + 1, last=last + 1)},
                        {
                            "role": "user",
                            "content": f"Document: {title}\n\n{preview(text, blocks, first, last)}",
                        },
                    ],
                    model=model or None,
                    max_tokens=INDEX_MAX_TOKENS,
                )
            data = _json_object(raw)
            found = repair(data.get("sections") if data else None, first, last)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - this window falls back to its headings
            log.warning("Indexing blocks %d–%d of %r failed: %s", first + 1, last + 1, title, exc)
        if not found:
            fallbacks += 1
            window = heading_sections(text, blocks[first : last + 1])
            found = [Section(s.first + first, s.last + first, s.title) for s in window]
        sections.extend(found)
    if fallbacks and fallbacks == len(windows(text, blocks)):
        raise IndexingError("the language model returned no usable sections")
    return sections, fallbacks


async def judge(
    chat: LLMChat,
    question: str,
    passages: Sequence[tuple[str, str]],
    *,
    model: str | None = None,
) -> list[int] | None:
    """Indices of the passages (label, text) that help answer, best first; None if unsure."""
    if not passages:
        return []
    listing = "\n\n".join(
        f"[{number}] {label}\n{body[:JUDGE_PREVIEW_CHARS]}{' …' if len(body) > JUDGE_PREVIEW_CHARS else ''}"
        for number, (label, body) in enumerate(passages, start=1)
    )
    try:
        async with asyncio.timeout(JUDGE_TIMEOUT_SECONDS):
            raw = await chat.complete(
                [
                    {"role": "system", "content": JUDGE_PROMPT},
                    {"role": "user", "content": f"Question: {question}\n\nPassages:\n\n{listing}"},
                ],
                model=model or None,
                max_tokens=200,
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - keep the search's own order
        log.info("Could not have the passages judged: %s", exc)
        return None
    data = _json_object(raw)
    if data is None or not isinstance(data.get("passages"), list):
        return None
    picked: list[int] = []
    for item in data["passages"]:
        try:
            index = int(item) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(passages) and index not in picked:
            picked.append(index)
    return picked
