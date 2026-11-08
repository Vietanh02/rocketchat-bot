"""Cutting a reply that is too long for one Rocket.Chat message.

Rocket.Chat refuses messages longer than `Message_MaxAllowedSize` (5000 by
default), so a long answer is posted as several messages. Cuts fall between
paragraphs where possible, else between lines, else between words. A cut
inside a code block closes the block in one message and reopens it, with the
same language, in the next, so both render as code.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

# Below Rocket.Chat's default 5000, leaving room for the streaming cursor and
# a closing code fence.
MAX_MESSAGE_CHARS = 4800
_FENCE = "```"
_CLOSE = "\n" + _FENCE


@dataclass(slots=True, frozen=True)
class Cut:
    """Where the first message ends."""

    # The first message, ready to post.
    head: str
    # How many characters of the input `head` used up.
    consumed: int
    # Put before the rest of the input to continue it (a reopened fence).
    reopen: str = ""


def first_cut(text: str, limit: int = MAX_MESSAGE_CHARS) -> Cut | None:
    """The first message of `text`, or None if it fits in one."""
    if len(text) <= limit:
        return None
    budget = max(1, limit - len(_CLOSE))
    paragraph = line = code_line = None  # best cut of each kind: (index, lang)
    in_code, lang = False, ""
    pos = 0
    while pos < budget:
        end = text.find("\n", pos)
        if end == -1 or end >= budget:
            break
        stripped = text[pos:end].strip()
        if stripped.startswith(_FENCE):
            if in_code:
                in_code, lang = False, ""
            else:
                in_code, lang = True, stripped[len(_FENCE):].strip()
        cut = end + 1
        if in_code:
            code_line = (cut, lang)
        else:
            line = (cut, "")
            if not stripped:
                paragraph = (cut, "")
        pos = cut

    # A cut very early in the window would make a lot of tiny messages.
    floor = budget // 3
    for candidate, inside in ((paragraph, False), (line, False), (code_line, True)):
        if candidate is not None and candidate[0] >= floor:
            return _cut(text, candidate[0], in_code=inside, lang=candidate[1])

    # One long line: cut between words, keeping the code state of that line.
    index = _last_space(text, max(pos, 1), budget)
    if index is None:
        index = budget
        while index > 1 and unicodedata.combining(text[index]):
            index -= 1
    return _cut(text, index, in_code=in_code, lang=lang)


def _cut(text: str, index: int, *, in_code: bool, lang: str) -> Cut:
    head = text[:index].rstrip()
    if in_code:
        return Cut(head + _CLOSE, index, f"{_FENCE}{lang}\n")
    # The next message starts at its first visible line.
    while index < len(text) and text[index] == "\n":
        index += 1
    return Cut(head, index)


def _last_space(text: str, start: int, end: int) -> int | None:
    for index in range(end, start, -1):
        if text[index - 1].isspace():
            return index
    return None


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """`text` as messages of at most `limit` characters each."""
    parts: list[str] = []
    rest = text
    while (cut := first_cut(rest, limit)) is not None:
        if cut.head.strip():
            parts.append(cut.head)
        rest = cut.reopen + rest[cut.consumed :]
    if rest.strip() or not parts:
        parts.append(rest)
    return parts
