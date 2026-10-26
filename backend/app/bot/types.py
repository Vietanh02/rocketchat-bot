"""Shared value objects passed through the bot pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.rocketchat.ddp import IncomingMessage


@dataclass(slots=True)
class MessageContext:
    """One inbound message plus everything a responder needs to answer it."""

    message_id: str
    room_id: str
    text: str
    sender_id: str | None
    sender_username: str | None
    room_type: str | None = None
    thread_id: str | None = None
    # Text with any bot @mention stripped, used for matching.
    clean_text: str = ""
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mentioned_bot: bool = False

    @property
    def is_direct(self) -> bool:
        return self.room_type == "d"

    @classmethod
    def from_incoming(
        cls, message: IncomingMessage, *, bot_username: str | None = None
    ) -> MessageContext:
        text = message.text or ""
        clean, mentioned = _strip_mention(text, bot_username)
        return cls(
            message_id=message.message_id,
            room_id=message.room_id,
            text=text,
            sender_id=message.sender_id,
            sender_username=message.sender_username,
            room_type=message.room_type,
            thread_id=message.thread_id,
            clean_text=clean,
            mentioned_bot=mentioned,
        )

    def template_vars(self) -> dict[str, Any]:
        """Variables exposed to portal-authored templates."""
        return {
            "user": {
                "id": self.sender_id,
                "username": self.sender_username or "",
                "mention": f"@{self.sender_username}" if self.sender_username else "",
            },
            "room": {"id": self.room_id, "type": self.room_type or ""},
            "message": {"id": self.message_id, "text": self.text},
            "now": self.received_at,
        }


def _strip_mention(text: str, bot_username: str | None) -> tuple[str, bool]:
    """Remove a leading/anywhere @bot mention so patterns can match cleanly."""
    stripped = text.strip()
    if not bot_username:
        return stripped, False
    token = f"@{bot_username}"
    if token.casefold() not in stripped.casefold():
        return stripped, False
    # Case-insensitive removal of every occurrence of the mention.
    out, low, low_token = [], stripped.casefold(), token.casefold()
    index = 0
    while True:
        found = low.find(low_token, index)
        if found == -1:
            out.append(stripped[index:])
            break
        out.append(stripped[index:found])
        index = found + len(token)
    return " ".join("".join(out).split()), True


@dataclass(slots=True)
class Reply:
    """What the bot wants to say back."""

    text: str
    thread_id: str | None = None
    room_id: str | None = None  # None means "same room as the trigger"
    command_name: str | None = None
    error: str | None = None
    # The sender is not allowed to use the bot; nothing is sent to them.
    denied: bool = False
