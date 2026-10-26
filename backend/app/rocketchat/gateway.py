"""The one seam between the bot logic and Rocket.Chat.

Everything above this module (router, responders, scheduler) talks to a
`Gateway`. Today the live implementation sends over REST; swapping to
Apps-Engine or anything else means adding one class here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.rocketchat.rest import RocketChatError, RocketChatRest

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SentMessage:
    room_id: str
    text: str
    thread_id: str | None = None
    message_id: str | None = None


@runtime_checkable
class Gateway(Protocol):
    """Outbound operations the bot needs from the chat platform."""

    async def send_message(
        self, room_id: str, text: str, *, thread_id: str | None = None
    ) -> SentMessage: ...

    async def update_message(self, room_id: str, message_id: str, text: str) -> None: ...

    async def resolve_dm_room(self, username: str) -> str: ...

    async def resolve_channel_room(self, name: str) -> str: ...

    async def aclose(self) -> None: ...


class LiveGateway:
    """REST-backed gateway against a real Rocket.Chat server."""

    def __init__(self, rest: RocketChatRest) -> None:
        self.rest = rest
        self._dm_cache: dict[str, str] = {}

    async def send_message(
        self, room_id: str, text: str, *, thread_id: str | None = None
    ) -> SentMessage:
        payload = await self.rest.post_message(room_id, text, thread_id=thread_id)
        message_id = (payload.get("message") or {}).get("_id")
        return SentMessage(room_id, text, thread_id, message_id)

    async def update_message(self, room_id: str, message_id: str, text: str) -> None:
        await self.rest.update_message(room_id, message_id, text)

    async def resolve_dm_room(self, username: str) -> str:
        cached = self._dm_cache.get(username)
        if cached:
            return cached
        room_id = await self.rest.create_dm(username)
        self._dm_cache[username] = room_id
        return room_id

    async def resolve_channel_room(self, name: str) -> str:
        return await self.rest.channel_id_by_name(name)

    async def aclose(self) -> None:
        await self.rest.aclose()


class FakeGateway:
    """In-memory gateway for tests and for the portal's dry-run mode."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.dm_rooms: dict[str, str] = {}
        self.channels: dict[str, str] = {}
        self.fail_next: Exception | None = None
        # Every edit, in order, as (message_id, text).
        self.updates: list[tuple[str, str]] = []
        self.fail_updates: Exception | None = None

    async def send_message(
        self, room_id: str, text: str, *, thread_id: str | None = None
    ) -> SentMessage:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
        message = SentMessage(room_id, text, thread_id, f"fake-{len(self.sent) + 1}")
        self.sent.append(message)
        return message

    async def update_message(self, room_id: str, message_id: str, text: str) -> None:
        if self.fail_updates is not None:
            raise self.fail_updates
        for index, message in enumerate(self.sent):
            if message.message_id == message_id:
                self.sent[index] = SentMessage(
                    message.room_id, text, message.thread_id, message_id
                )
                self.updates.append((message_id, text))
                return
        raise RocketChatError(f"no message {message_id!r}")

    async def resolve_dm_room(self, username: str) -> str:
        return self.dm_rooms.setdefault(username, f"dm-{username}")

    async def resolve_channel_room(self, name: str) -> str:
        key = name.lstrip("#")
        if key not in self.channels:
            raise RocketChatError(f"no channel named {name!r}")
        return self.channels[key]

    async def aclose(self) -> None:
        return None

    # test helper
    @property
    def last_text(self) -> str | None:
        return self.sent[-1].text if self.sent else None


@dataclass
class ConnectionStatus:
    """What the portal dashboard shows about the Rocket.Chat link."""

    configured: bool = False
    connected: bool = False
    bot_user_id: str | None = None
    bot_username: str | None = None
    rest_url: str | None = None
    ws_url: str | None = None
    last_error: str | None = None
    messages_handled: int = 0
    details: dict[str, str] = field(default_factory=dict)
