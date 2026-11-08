"""Is a message a question to look up, a request to do something, or small talk?

- **lookup**: asks for information ("nghỉ phép được bao nhiêu ngày?",
  "how do I reset my VPN password?"). The documents are searched and the
  passages found are put in front of the question, to be answered from
  and cited.
- **command**: asks the assistant to do something ("tạo task họp lúc 9h",
  "send the report to @an"). No documents are searched; the model is told
  to act with its tools, or say what it cannot do.
- **chat**: greetings and thanks. Nothing extra.

Patterns of the wording decide at once, in Vietnamese and English. A
message they cannot place is a lookup (searching costs little and the
model ignores passages that do not help), or, with the "model" mode, the
fast model is asked, in a few tokens.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.bot.llm import LLMChat

log = logging.getLogger(__name__)

LOOKUP, COMMAND, CHAT = "lookup", "command", "chat"
KINDS = (LOOKUP, COMMAND, CHAT)
CLASSIFY_TIMEOUT_SECONDS = 3.0


@dataclass(slots=True, frozen=True)
class Intent:
    kind: str
    # "rules", "model", or "default" when neither could tell.
    by: str = "rules"


def _words(*phrases: str) -> str:
    return "|".join(sorted((re.escape(p).replace(r"\ ", r"\s+") for p in phrases), key=len, reverse=True))


# Asking someone to do something, before the verb.
_POLITE = _words(
    "hãy", "bạn hãy", "em hãy", "giúp tôi", "giúp mình", "giúp em", "giúp anh", "giúp chị",
    "bạn giúp tôi", "bạn giúp mình", "nhờ bạn", "nhờ", "làm ơn", "vui lòng", "xin hãy",
    "please", "pls", "kindly", "can you", "could you", "would you", "will you", "can u",
    "i want you to", "i need you to",
)
_COMMAND_VERBS = _words(
    # Vietnamese
    "tạo", "thêm", "xoá", "xóa", "gỡ", "sửa", "cập nhật", "đổi", "chỉnh", "đặt", "đặt lịch",
    "lên lịch", "hẹn", "nhắc", "gửi", "chuyển", "chạy", "khởi động", "khởi động lại", "dừng",
    "tắt", "bật", "huỷ", "hủy", "duyệt", "phê duyệt", "đăng", "ghi", "ghi lại", "lưu", "nhớ",
    "quên", "đăng ký", "kiểm tra", "triển khai", "tải", "tải lên", "xuất", "nhập", "viết",
    "soạn", "dịch", "gán", "giao", "mời", "thông báo", "đóng", "mở", "reset", "deploy",
    # English
    "create", "add", "delete", "remove", "update", "change", "edit", "set", "schedule", "remind",
    "send", "post", "forward", "run", "start", "stop", "restart", "deploy", "open", "close",
    "cancel", "book", "enable", "disable", "turn on", "turn off", "save", "remember", "forget",
    "write", "draft", "translate", "generate", "make", "assign", "approve", "upload", "export",
    "import", "invite", "notify", "check", "rename", "move", "copy", "install",
)
# Asking how to do something is a question, even when it starts with a verb.
_HOW_TO = re.compile(
    r"\b(" + _words(
        "như thế nào", "thế nào", "ra sao", "làm sao", "làm thế nào", "bằng cách nào", "cách nào",
        "cách", "là gì", "có được không", "được không", "how to", "how do", "how can", "how should",
        "what is", "what are", "is it possible",
    ) + r")\b"
)
_COMMAND = re.compile(rf"^(?:(?:{_POLITE})\s+)?(?:{_COMMAND_VERBS})\b")
_LOOKUP = re.compile(
    r"(\?\s*$)|\b("
    + _words(
        # Vietnamese question words and look-up verbs
        "là gì", "là ai", "gì", "nào", "ai", "đâu", "bao nhiêu", "bao lâu", "bao giờ", "khi nào",
        "lúc nào", "tại sao", "vì sao", "sao", "thế nào", "ra sao", "làm sao", "có phải",
        "hay không", "không ạ", "cho tôi biết", "cho mình biết", "cho em biết", "cho tôi hỏi",
        "cho mình hỏi", "cho em hỏi", "tra cứu", "tìm hiểu", "tìm", "giải thích", "tóm tắt",
        "liệt kê", "hướng dẫn", "quy định", "chính sách", "thông tin", "nghĩa là", "khác nhau",
        "so sánh", "trong tài liệu", "theo tài liệu",
        # English
        "what", "who", "whom", "whose", "which", "where", "when", "why", "how", "explain",
        "summarize", "summarise", "summary", "describe", "define", "tell me", "look up", "find out",
        "search", "find", "list", "compare", "difference", "according to", "policy", "is there",
        "are there", "do we", "does", "meaning",
    )
    + r")\b"
)
_STARTS_AS_QUESTION = re.compile(
    r"^(is|are|was|were|do|does|did|can i|can we|should|could i|may i|have|has|có)\b"
)
_CHAT = re.compile(
    r"^(" + _words(
        "xin chào", "chào", "chào bạn", "chào bot", "hi", "hello", "hey", "yo", "good morning",
        "good afternoon", "good evening", "cảm ơn", "cám ơn", "cảm ơn bạn", "thanks", "thank you",
        "thx", "ok", "okay", "oke", "ok bạn", "được rồi", "tốt", "tuyệt", "great", "nice", "bye",
        "tạm biệt", "hihi", "haha", "hehe", "👍", "ừ", "ừm", "vâng", "dạ",
    ) + r")[\s!.,~:)(]*$"
)
# "this document", "file này", ...: the question is about a document sent.
_ABOUT_A_DOCUMENT = re.compile(
    r"\b(" + _words(
        "tài liệu", "văn bản", "file", "tệp", "báo cáo", "hợp đồng", "bản này", "đoạn này",
        "document", "doc", "pdf", "attachment", "report", "this file", "the file",
    ) + r")\b"
)
_LEADING_NOISE = re.compile(r"^(?:@[\w.-]+[:,]?\s*)+")


def normalize(text: str) -> str:
    text = _LEADING_NOISE.sub("", text.strip())
    return " ".join(text.casefold().split())


def about_a_document(text: str) -> bool:
    """Whether the message talks about a document ("tóm tắt tài liệu này")."""
    return bool(_ABOUT_A_DOCUMENT.search(normalize(text)))


def classify(text: str) -> Intent | None:
    """The intent the wording shows, or None if it does not tell."""
    words = normalize(text)
    if not words:
        return Intent(CHAT)
    if _CHAT.match(words):
        return Intent(CHAT)
    if _COMMAND.match(words) and not _HOW_TO.search(words):
        return Intent(COMMAND)
    if _LOOKUP.search(words) or _STARTS_AS_QUESTION.match(words):
        return Intent(LOOKUP)
    return None


CLASSIFY_PROMPT = (
    "Classify the user's chat message. Reply with exactly one word:\n"
    "LOOKUP - it asks for information, an explanation, or what a document says.\n"
    "COMMAND - it asks the assistant to do something: create, change, send, schedule, "
    "run, write or remember something.\n"
    "CHAT - a greeting, thanks or small talk."
)


async def detect(
    text: str, *, mode: str = "rules", chat: LLMChat | None = None, fast_model: str = ""
) -> Intent:
    """The message's intent, by the patterns, then by the fast model if `mode` is "model"."""
    found = classify(text)
    if found is not None:
        return found
    if mode == "model" and chat is not None:
        import asyncio

        try:
            async with asyncio.timeout(CLASSIFY_TIMEOUT_SECONDS):
                answer = await chat.complete(
                    [
                        {"role": "system", "content": CLASSIFY_PROMPT},
                        {"role": "user", "content": text[:2000]},
                    ],
                    model=fast_model or None,
                    max_tokens=5,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fall back to the default
            log.info("Could not classify a message with the model: %s", exc)
        else:
            word = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip().upper()
            for kind in KINDS:
                if word.startswith(kind.upper()):
                    return Intent(kind, "model")
    return Intent(LOOKUP, "default")
