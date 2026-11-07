"""Rewrites a language model's Markdown into what Rocket.Chat renders.

Models write CommonMark/GitHub Markdown. Rocket.Chat's message syntax is close
but not the same, and the differences show up as stray symbols or the wrong
emphasis:

    model writes              Rocket.Chat needs
    **bold**, __bold__        *bold*
    *italic*                  _italic_          (in Rocket.Chat *x* is bold)
    ***both***                *_both_*
    ~~strike~~                ~strike~
    # Heading                 *Heading*
    * item, + item            - item
    | table |                 an aligned table in a code block
    ![alt](url)               [alt](url)
    ---                       a line
    <br>                      a line break
    ~~~ fences                ``` fences
    <think>…</think>          (removed: a reasoning model's scratchpad)

Code, inline code and URLs are left alone. Links, quotes, numbered lists,
task lists, _italic_ and `code` already mean the same thing in both.

`partial=True` is for a reply that is still streaming in. It differs only
where the end of the text is unfinished: an open <think> block hides
everything after it, and table rows are held back until the table is known to
be one, so the message does not flicker between raw pipes and a table.
"""

from __future__ import annotations

import re
import unicodedata

_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.IGNORECASE | re.DOTALL)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"</think>\s*", re.IGNORECASE)
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")
_RULE = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET = re.compile(r"^(\s*)[*+](\s+)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Spans whose contents are never rewritten.
_PROTECTED = re.compile(
    r"`[^`\n]+`"                    # inline code
    r"|\]\([^)\s]+(?:\s+\"[^\"]*\")?\)"  # a link's target
    r"|<?https?://[^\s<>]+>?"       # a bare URL
)
_IMAGE = re.compile(r"!\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BOLD_ITALIC = re.compile(r"\*\*\*(?=\S)(.+?)(?<=\S)\*\*\*")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![*\w])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

# Stand-ins while emphasis is rewritten, so new *bold* is not read as italic.
_STAR = "\x00"
_SLOT = "\x01{}\x01"
_SLOT_RE = re.compile("\x01(\\d+)\x01")

RULE_LINE = "────────────"


def to_rocketchat(text: str, *, partial: bool = False) -> str:
    """`text` with its Markdown rewritten for Rocket.Chat."""
    text = strip_thinking(text, partial=partial)
    lines = text.split("\n")
    if partial:
        if len(lines) > 1 and not lines[-1].strip():
            lines.pop()  # the next line has not started yet
        if lines[-1].lstrip().startswith("|"):
            lines.pop()  # a table row still arriving, perhaps cut off mid-cell
    out: list[str] = []
    index = 0
    fence: str | None = None  # the closing marker while inside a code block

    while index < len(lines):
        line = lines[index]

        if fence is not None:
            match = _FENCE.match(line)
            if match and match.group(2)[0] == fence[0] and len(match.group(2)) >= len(fence):
                out.append(match.group(1) + "```")
                fence = None
            else:
                out.append(line)
            index += 1
            continue

        match = _FENCE.match(line)
        if match:
            fence = match.group(2)
            out.append(match.group(1) + "```" + match.group(3).strip())
            index += 1
            continue

        if _TABLE_ROW.match(line):
            end = index + 1
            while end < len(lines) and (
                _TABLE_ROW.match(lines[end])
                or (end == index + 1 and _TABLE_RULE.match(lines[end]))
            ):
                end += 1
            rows = lines[index:end]
            if len(rows) >= 2 and _TABLE_RULE.match(rows[1]):
                out.extend(_table(rows))
                index = end
                continue
            if partial and end == len(lines):
                # Maybe a table's header; the next line will tell.
                break

        out.append(_line(line))
        index += 1

    return "\n".join(out).strip("\n")


def strip_thinking(text: str, *, partial: bool = False) -> str:
    """`text` without a reasoning model's <think> block."""
    text = _THINK_BLOCK.sub("", text)
    # Some gateways put <think> in the prompt template, so only the end arrives.
    close = _THINK_CLOSE.search(text)
    if close is not None:
        text = text[close.end() :].lstrip()
    match = _THINK_OPEN.search(text)
    if match is None:
        return text
    before = text[: match.start()]
    if partial or before.strip():
        return before
    # The whole answer is one unclosed <think>: show it rather than nothing.
    return text[match.end() :].lstrip()


def _line(line: str) -> str:
    heading = _HEADING.match(line)
    if heading:
        title = _inline(heading.group(1)).replace("*", "").strip()
        return f"*{title}*" if title else ""
    if _RULE.match(line):
        return RULE_LINE
    line = _BULLET.sub(r"\1-\2", line)
    return _inline(line)


def _inline(text: str) -> str:
    text = _BR.sub("\n", text)
    text = _IMAGE.sub(lambda m: f"[{m.group(1) or m.group(2)}]({m.group(2)})", text)

    kept: list[str] = []

    def keep(match: re.Match) -> str:
        kept.append(match.group(0))
        return _SLOT.format(len(kept) - 1)

    text = _PROTECTED.sub(keep, text)
    text = _BOLD_ITALIC.sub(lambda m: f"{_STAR}_{m.group(1)}_{_STAR}", text)
    text = _BOLD.sub(lambda m: f"{_STAR}{m.group(1) or m.group(2)}{_STAR}", text)
    text = _ITALIC.sub(r"_\1_", text)
    text = _STRIKE.sub(r"~\1~", text)
    text = text.replace(_STAR, "*")
    return _SLOT_RE.sub(lambda m: kept[int(m.group(1))], text)


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #


def _table(rows: list[str]) -> list[str]:
    """A Markdown table as aligned columns in a code block."""
    header = _cells(rows[0])
    body = [_cells(row) for row in rows[2:]]
    columns = max(len(header), *(len(row) for row in body)) if body else len(header)
    grid = [row + [""] * (columns - len(row)) for row in [header, *body]]
    widths = [max(_width(row[col]) for row in grid) for col in range(columns)]

    def render(row: list[str]) -> str:
        return " | ".join(_pad(cell, widths[col]) for col, cell in enumerate(row)).rstrip()

    lines = ["```", render(grid[0]), "-+-".join("-" * width for width in widths)]
    lines.extend(render(row) for row in grid[1:])
    lines.append("```")
    return lines


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    cells = re.split(r"(?<!\\)\|", row)
    return [_plain(cell.strip().replace("\\|", "|")) for cell in cells]


def _plain(cell: str) -> str:
    """A cell's text without Markdown, since a code block shows it literally."""
    cell = _BR.sub(" ", cell)
    cell = _IMAGE.sub(lambda m: m.group(1) or m.group(2), cell)
    cell = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r"\1 (\2)", cell)
    cell = re.sub(r"(\*\*|__|~~|`)(.+?)\1", r"\2", cell)
    cell = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])", r"\1", cell)
    return cell


def _width(text: str) -> int:
    """Columns `text` takes in a monospace font."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))
