"""Markdown from a model, rewritten into Rocket.Chat's message syntax."""

from __future__ import annotations

import pytest

from app.bot.rcformat import RULE_LINE, strip_thinking, to_rocketchat


@pytest.mark.parametrize(
    ("markdown", "expected"),
    [
        ("**bold** and __bold__", "*bold* and *bold*"),
        ("*italic* and _italic_", "_italic_ and _italic_"),
        ("***both***", "*_both_*"),
        ("~~gone~~", "~gone~"),
        ("**Lưu ý:** *quan trọng*", "*Lưu ý:* _quan trọng_"),
        ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),
        ("snake_case_name stays", "snake_case_name stays"),
        ("`**code**` stays", "`**code**` stays"),
        ("see https://x.com/__init__*a*", "see https://x.com/__init__*a*"),
        ("[**docs**](https://x.com/a_b_c)", "[*docs*](https://x.com/a_b_c)"),
        ("![diagram](https://x.com/d.png)", "[diagram](https://x.com/d.png)"),
        ("![](https://x.com/d.png)", "[https://x.com/d.png](https://x.com/d.png)"),
        ("one<br>two<br/>three", "one\ntwo\nthree"),
        ("> quoted **text**", "> quoted *text*"),
        ("1. first\n2. second", "1. first\n2. second"),
        ("- [ ] todo\n- [x] done", "- [ ] todo\n- [x] done"),
    ],
)
def test_inline_syntax(markdown, expected):
    assert to_rocketchat(markdown) == expected


def test_headings_become_bold_lines():
    assert to_rocketchat("# Title\n## **Sub** title ##\ntext") == "*Title*\n*Sub title*\ntext"


def test_bullets_use_dashes_and_keep_nesting():
    assert to_rocketchat("* a\n  + b\n    - c") == "- a\n  - b\n    - c"


def test_rules_become_a_line():
    assert to_rocketchat("a\n\n---\n\n***\nb") == f"a\n\n{RULE_LINE}\n\n{RULE_LINE}\nb"


def test_code_blocks_are_untouched_and_tilde_fences_converted():
    text = "~~~python\nx = 2 ** 3  # **not bold**\n* not a bullet\n~~~\nafter **b**"
    assert to_rocketchat(text) == (
        "```python\nx = 2 ** 3  # **not bold**\n* not a bullet\n```\nafter *b*"
    )


def test_a_table_becomes_aligned_columns_in_a_code_block():
    text = "Kết quả:\n\n| Tên | **Tuổi** |\n|:---|---:|\n| An Nguyễn | 30 |\n| 日本 | 5 |\n\nHết."
    assert to_rocketchat(text) == (
        "Kết quả:\n\n"
        "```\n"
        "Tên       | Tuổi\n"
        "----------+-----\n"
        "An Nguyễn | 30\n"
        "日本      | 5\n"
        "```\n\n"
        "Hết."
    )


def test_table_cells_lose_their_markup_and_keep_escaped_pipes():
    text = "| a | b |\n|---|---|\n| [x](https://x.com) | 1 \\| 2 |"
    assert to_rocketchat(text).split("\n") == [
        "```",
        "a                 | b",
        "------------------+------",
        "x (https://x.com) | 1 | 2",
        "```",
    ]


def test_pipes_that_are_not_a_table_are_left_alone():
    assert to_rocketchat("| just a line |\nnext") == "| just a line |\nnext"


def test_thinking_is_removed():
    assert to_rocketchat("<think>\nlet me see\n</think>\n\nAnswer **here**") == "Answer *here*"
    assert to_rocketchat("only the end arrived</think>Answer") == "Answer"
    assert strip_thinking("<THINK>a</THINK>b") == "b"


def test_an_answer_that_is_all_unclosed_thinking_is_still_shown():
    assert to_rocketchat("<think>I never closed this") == "I never closed this"


# --------------------------------------------------------------------------- #
# While streaming
# --------------------------------------------------------------------------- #


def test_partial_thinking_shows_nothing_yet():
    assert to_rocketchat("<think>still going", partial=True) == ""
    assert to_rocketchat("Hi <think>still going", partial=True) == "Hi "


def test_partial_table_rows_wait_until_complete():
    assert to_rocketchat("Bảng:\n| a | b |", partial=True) == "Bảng:"
    assert to_rocketchat("Bảng:\n| a | b |\n", partial=True) == "Bảng:"
    assert to_rocketchat("Bảng:\n| a | b |\n|---|---|\n| 1 | 2 |\n| 3 ", partial=True) == (
        "Bảng:\n```\na | b\n--+--\n1 | 2\n```"
    )


def test_streaming_mostly_grows_by_appending():
    """Each partial is a prefix of the final text, so the live message only grows."""
    final = "Đây là **đậm**, *nghiêng* và `code`.\n\n* một\n* hai\n\n```py\nx = 1\n```\nXong."
    expected = to_rocketchat(final)
    regressions = 0
    for cut in range(1, len(final)):
        if not expected.startswith(to_rocketchat(final[:cut], partial=True).rstrip()):
            regressions += 1
    # Only the moments inside an unclosed ** or * differ.
    assert regressions < len(final) // 4
