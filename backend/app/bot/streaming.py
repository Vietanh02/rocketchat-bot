"""Delivers a reply either whole, or as a message that grows in place.

Rocket.Chat has no native streaming, so "stream" mode posts the first chunk
and then edits that same message (`chat.update`) until it holds the full
reply. The edits come back to us on the DDP stream as edits from the bot
itself, which the runner already ignores.

The number of edits is capped, so a long reply grows in bigger chunks rather
than taking longer: the message loop handles one reply at a time, and a slow
stream would hold up everyone else's answers.
"""

from __future__ import annotations

import asyncio
import logging
import math
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.models import ReplyMode
from app.rocketchat.gateway import Gateway, SentMessage
from app.rocketchat.rest import RocketChatError

log = logging.getLogger(__name__)

# Shown at the end of the message while it is still growing.
STREAM_CURSOR = " ▍"
# Upper bounds for one streamed reply, whatever the portal settings say.
MAX_STREAM_EDITS = 40
MAX_STREAM_SECONDS = 8.0

Sleep = Callable[[float], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class DeliveryOptions:
    mode: str = ReplyMode.whole.value
    chunk_chars: int = 24
    interval_ms: int = 250

    @property
    def streaming(self) -> bool:
        return self.mode == ReplyMode.stream.value

    @classmethod
    def from_settings(cls, settings) -> DeliveryOptions:
        """Build from a `BotSettingsSnapshot`."""
        return cls(
            mode=settings.reply_mode,
            chunk_chars=max(1, settings.stream_chunk_chars),
            interval_ms=max(0, settings.stream_interval_ms),
        )


def stream_steps(text: str, chunk_chars: int, max_steps: int) -> list[str]:
    """Growing prefixes of `text`, the last being `text` itself.

    Cuts fall on whitespace where a chunk contains any, so words appear
    whole; a word longer than a chunk is split, but never inside a
    combining sequence (Vietnamese tone marks, for instance).
    """
    if not text:
        return [text]
    chunk = max(chunk_chars, math.ceil(len(text) / max(1, max_steps)), 1)
    steps: list[str] = []
    pos = 0
    while True:
        target = pos + chunk
        if target >= len(text):
            steps.append(text)
            return steps
        cut = _last_space(text, pos, target)
        if cut is None:
            cut = target
            while cut < len(text) and unicodedata.combining(text[cut]):
                cut += 1
        if cut >= len(text):
            steps.append(text)
            return steps
        steps.append(text[:cut])
        pos = cut


def _last_space(text: str, start: int, end: int) -> int | None:
    """Index just after the last whitespace in text[start:end], if any."""
    for index in range(end, start, -1):
        if text[index - 1].isspace():
            return index
    return None


def render_partial(prefix: str) -> str:
    """What the message shows mid-stream: the prefix plus a cursor.

    An unclosed code fence is closed temporarily, so the rest of the message
    does not flicker in and out of code formatting while it grows.
    """
    shown = prefix.rstrip() + STREAM_CURSOR
    if prefix.count("```") % 2:
        shown = prefix.rstrip() + "\n```" + STREAM_CURSOR
    return shown


async def deliver(
    gateway: Gateway,
    room_id: str,
    text: str,
    *,
    thread_id: str | None = None,
    options: DeliveryOptions | None = None,
    sleep: Sleep = asyncio.sleep,
) -> SentMessage:
    """Send `text` to a room, whole or streamed according to `options`."""
    options = options or DeliveryOptions()
    if not options.streaming:
        return await gateway.send_message(room_id, text, thread_id=thread_id)

    interval = options.interval_ms / 1000
    budget = math.floor(MAX_STREAM_SECONDS / interval) if interval else MAX_STREAM_EDITS
    max_steps = 1 + max(1, min(MAX_STREAM_EDITS, budget))
    steps = stream_steps(text, options.chunk_chars, max_steps)
    if len(steps) == 1:
        return await gateway.send_message(room_id, text, thread_id=thread_id)

    first = await gateway.send_message(
        room_id, render_partial(steps[0]), thread_id=thread_id
    )
    message_id = first.message_id
    if not message_id:
        # Nothing to edit; make sure the user still gets the whole answer.
        log.warning("Rocket.Chat returned no message id; sending the reply whole")
        return await gateway.send_message(room_id, text, thread_id=thread_id)

    for index, step in enumerate(steps[1:], start=1):
        await sleep(interval)
        shown = step if index == len(steps) - 1 else render_partial(step)
        try:
            await gateway.update_message(room_id, message_id, shown)
        except RocketChatError as exc:
            log.warning("Streaming edit failed (%s); finishing the reply at once", exc)
            return await _finish(gateway, room_id, message_id, text, thread_id)

    return SentMessage(room_id, text, thread_id, message_id)


async def _finish(
    gateway: Gateway,
    room_id: str,
    message_id: str,
    text: str,
    thread_id: str | None,
) -> SentMessage:
    """After a failed edit: one more try at the full text, else a new message."""
    try:
        await gateway.update_message(room_id, message_id, text)
        return SentMessage(room_id, text, thread_id, message_id)
    except RocketChatError:
        # Editing is probably disabled for the bot. The partial message stays,
        # but the full reply follows it.
        return await gateway.send_message(room_id, text, thread_id=thread_id)
