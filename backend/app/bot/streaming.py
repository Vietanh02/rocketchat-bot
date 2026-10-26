"""Delivers a reply either whole, or as a message that grows in place.

Rocket.Chat has no native streaming, so "stream" mode posts the first chunk
and then edits that same message (`chat.update`) until it holds the full
reply. The edits come back to us on the DDP stream as edits from the bot
itself, which the runner already ignores.

The number of edits is capped, so a long reply grows in bigger chunks rather
than taking longer: the message loop handles one reply at a time, and a slow
stream would hold up everyone else's answers.

`LiveReply` is the other kind of stream: text that is still being produced,
such as a script's output, shown as it arrives rather than replayed afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
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
# A live reply is edited at most this often (the portal's lowest delay), so a
# long-running script cannot flood Rocket.Chat with edits.
LIVE_MIN_INTERVAL_SECONDS = 0.05

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
        # Whitespace at the start of a chunk is free, or a step could add nothing visible.
        start = pos
        while start < len(text) and text[start].isspace():
            start += 1
        target = start + chunk
        if target >= len(text):
            steps.append(text)
            return steps
        cut = _last_space(text, start, target)
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
    steps = stream_steps(text, options.chunk_chars, 1 + _max_edits(interval))
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


def _max_edits(interval: float) -> int:
    """How many edits fit in one streamed reply at this interval."""
    budget = math.floor(MAX_STREAM_SECONDS / interval) if interval else MAX_STREAM_EDITS
    return max(1, min(MAX_STREAM_EDITS, budget))


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


class LiveReply:
    """A reply shown while it is still being produced, such as a script's output.

    `push` hands over the text so far. It is typed out like a streamed reply:
    every interval the message is edited to show the next `chunk_chars` of it,
    cut between words. If the text arrives faster than that, the chunks grow so
    the message catches up within `MAX_STREAM_EDITS` edits. `finish` types out
    whatever is left of the final text within the usual streaming limits, or
    streams it from scratch if nothing was posted yet.
    """

    def __init__(
        self,
        gateway: Gateway,
        room_id: str,
        *,
        thread_id: str | None = None,
        options: DeliveryOptions | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._options = options or DeliveryOptions(mode=ReplyMode.stream.value)
        self._gateway = gateway
        self._room_id = room_id
        self._thread_id = thread_id
        self._sleep = sleep
        self._interval = max(self._options.interval_ms / 1000, LIVE_MIN_INTERVAL_SECONDS)
        self._latest = ""
        self._shown = ""
        self._message_id: str | None = None
        self._shown_at = -math.inf
        self._closed = False
        self._failed = False
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def output(self) -> str:
        """Everything pushed so far, whether or not it has been typed out yet."""
        return self._latest

    def push(self, text: str) -> None:
        if self._closed or self._failed or not text.strip():
            return
        self._latest = text
        self._wake.set()
        if self._task is None:
            self._task = asyncio.create_task(self._type_along(), name="live-reply")

    async def _type_along(self) -> None:
        """Reveal pushed text one chunk per interval until `finish`."""
        loop = asyncio.get_running_loop()
        while not self._closed:
            wait = self._shown_at + self._interval - loop.time()
            if self._latest == self._shown or wait > 0:
                # A push or `finish` wakes this early; nothing waits out the interval.
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), None if self._latest == self._shown else wait
                    )
                continue
            if not await self._show(self._next_step(), partial=True):
                return

    def _next_step(self) -> str:
        """The shown text plus the next chunk of what has been pushed."""
        if not self._latest.startswith(self._shown):
            return self._latest
        rest = self._latest[len(self._shown) :]
        return self._shown + stream_steps(rest, self._options.chunk_chars, MAX_STREAM_EDITS)[0]

    async def _show(self, text: str, *, partial: bool) -> bool:
        """Post or edit the message to show `text`; False if that failed."""
        shown = render_partial(text) if partial else text
        try:
            if self._message_id is None:
                sent = await self._gateway.send_message(
                    self._room_id, shown, thread_id=self._thread_id
                )
                self._message_id = sent.message_id
            else:
                await self._gateway.update_message(self._room_id, self._message_id, shown)
        except RocketChatError as exc:
            log.warning("Live reply edit failed (%s); the full reply follows at the end", exc)
            self._failed = True
            return False
        self._shown, self._shown_at = text, asyncio.get_running_loop().time()
        if self._message_id is None:
            log.warning("Rocket.Chat returned no message id; the full reply follows at the end")
            self._failed = True
            return False
        return True

    async def finish(self, text: str) -> SentMessage:
        """Type out the rest of `text`, the complete reply, in the live message."""
        self._closed = True
        self._wake.set()
        if self._task is not None:
            await self._task
        if self._message_id is None:
            return await deliver(
                self._gateway,
                self._room_id,
                text,
                thread_id=self._thread_id,
                options=self._options,
                sleep=self._sleep,
            )
        if self._failed or not text.startswith(self._shown):
            return await _finish(
                self._gateway, self._room_id, self._message_id, text, self._thread_id
            )

        shown = self._shown
        max_edits = _max_edits(self._interval)
        steps = stream_steps(text[len(shown) :], self._options.chunk_chars, max_edits)
        loop = asyncio.get_running_loop()
        # Cuts between words can add a step; the last edit then makes up for it.
        for step in steps[:-1][: max_edits - 1]:
            await self._sleep(max(0.0, self._shown_at + self._interval - loop.time()))
            if not await self._show(shown + step, partial=True):
                break
        else:
            await self._sleep(max(0.0, self._shown_at + self._interval - loop.time()))
        return await _finish(self._gateway, self._room_id, self._message_id, text, self._thread_id)

    def abort(self) -> None:
        """Stop showing updates; for when no final reply will come."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
