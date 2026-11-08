"""One turn per conversation at a time.

Two people talking to the bot in the same channel share one conversation, so
their questions must not be answered at the same time: each would be sent a
transcript missing the other's exchange. Every turn is therefore submitted
under its conversation's key and run after the turns before it.

The queue mode says what happens to messages that arrive while a turn runs:

- "followup": each gets its own turn afterwards, in order.
- "collect": all that piled up are answered together in one turn. The last
  submitter gets the result; the others get `MERGED`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# Turns one conversation may have waiting; more are refused.
MAX_PENDING = 20


class _Merged:
    def __repr__(self) -> str:
        return "MERGED"


# What a submitter gets when its message was answered as part of a later one.
MERGED: Any = _Merged()


class QueueFull(Exception):
    """The conversation already has too many turns waiting."""


Handler = Callable[[list[T]], Awaitable[R]]


@dataclass(slots=True)
class _Entry(Generic[T]):
    item: T
    handler: Callable[[list[T]], Awaitable[Any]]
    future: asyncio.Future
    collect: bool


@dataclass(slots=True)
class _Lane:
    pending: list[_Entry] = field(default_factory=list)
    task: asyncio.Task | None = None


class SessionQueue:
    def __init__(self, max_pending: int = MAX_PENDING) -> None:
        self._lanes: dict[str, _Lane] = {}
        self._max_pending = max_pending

    def busy(self, key: str) -> bool:
        return key in self._lanes

    def waiting(self) -> int:
        return sum(len(lane.pending) for lane in self._lanes.values())

    async def submit(
        self,
        key: str,
        item: T,
        handler: Callable[[list[T]], Awaitable[R]],
        *,
        collect: bool = False,
    ) -> R:
        """Run `handler([item, ...])` once the conversation is free; its result."""
        lane = self._lanes.get(key)
        if lane is None:
            lane = self._lanes[key] = _Lane()
        if len(lane.pending) >= self._max_pending:
            raise QueueFull(key)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        lane.pending.append(_Entry(item, handler, future, collect))
        if lane.task is None:
            lane.task = asyncio.create_task(self._drain(key, lane), name=f"session-{key}")
        return await future

    async def _drain(self, key: str, lane: _Lane) -> None:
        try:
            while lane.pending:
                first = lane.pending[0]
                if first.collect:
                    # Everything waiting that also collects goes in this turn.
                    batch = []
                    while lane.pending and lane.pending[0].collect:
                        batch.append(lane.pending.pop(0))
                else:
                    batch = [lane.pending.pop(0)]
                live = [entry for entry in batch if not entry.future.cancelled()]
                for entry in batch:
                    if entry not in live:
                        log.debug("A turn of %s was cancelled before it ran", key)
                if not live:
                    continue
                last = live[-1]
                try:
                    result = await last.handler([entry.item for entry in live])
                except asyncio.CancelledError:
                    for entry in live:
                        if not entry.future.done():
                            entry.future.cancel()
                    raise
                except Exception as exc:  # noqa: BLE001 - handed to the submitter
                    if not last.future.done():
                        last.future.set_exception(exc)
                    for entry in live[:-1]:
                        if not entry.future.done():
                            entry.future.set_result(MERGED)
                    continue
                for entry in live[:-1]:
                    if not entry.future.done():
                        entry.future.set_result(MERGED)
                if not last.future.done():
                    last.future.set_result(result)
        finally:
            # No await between the empty check and here, so nothing slips in unseen.
            if self._lanes.get(key) is lane:
                del self._lanes[key]
            for entry in lane.pending:
                if not entry.future.done():
                    entry.future.cancel()

    async def close(self) -> None:
        """Cancel every running and waiting turn."""
        tasks = [lane.task for lane in self._lanes.values() if lane.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._lanes.clear()
