"""Where the time of one turn went.

Every turn carries a `TurnTimer`. It records marks (when the reaction went
out, when the first token arrived, ...) and, separately, the time spent
waiting on something outside this process: the model, a tool, Rocket.Chat or
the database. What is left of the total is the bot's own work, the
"overhead", which is the number that says whether rewriting it in a faster
language could make answers noticeably quicker.
"""

from __future__ import annotations

import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# Kinds of waiting that are not this process's own work.
WAIT_KINDS = ("llm", "tools", "rc", "db")


@dataclass(slots=True)
class ToolTiming:
    name: str
    ms: float
    ok: bool


@dataclass(slots=True)
class TurnTimer:
    # time.monotonic() when the message reached the bot.
    started: float = field(default_factory=time.monotonic)
    marks: dict[str, float] = field(default_factory=dict)
    waits: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    tools: list[ToolTiming] = field(default_factory=list)
    rounds: int = 0
    ended: float | None = None

    def now_ms(self) -> float:
        return (time.monotonic() - self.started) * 1000

    def mark(self, name: str) -> None:
        """Record when `name` happened; the first time counts."""
        self.marks.setdefault(name, self.now_ms())

    def mark_at(self, name: str, monotonic: float) -> None:
        self.marks.setdefault(name, (monotonic - self.started) * 1000)

    def add_wait(self, kind: str, seconds: float) -> None:
        self.waits[kind] += seconds * 1000

    @contextmanager
    def waiting(self, kind: str):
        """Count the time inside the block as waiting on `kind`."""
        begin = time.monotonic()
        try:
            yield
        finally:
            self.add_wait(kind, time.monotonic() - begin)

    def finish(self) -> None:
        if self.ended is None:
            self.ended = time.monotonic()

    @property
    def total_ms(self) -> float:
        end = self.ended if self.ended is not None else time.monotonic()
        return (end - self.started) * 1000

    @property
    def overhead_ms(self) -> float:
        """The total minus all waiting: the bot's own work."""
        return max(0.0, self.total_ms - sum(self.waits.values()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "marks": {name: round(ms, 2) for name, ms in self.marks.items()},
            "waits": {name: round(ms, 2) for name, ms in self.waits.items()},
            "tools": [
                {"name": tool.name, "ms": round(tool.ms, 2), "ok": tool.ok} for tool in self.tools
            ],
            "rounds": self.rounds,
            "total_ms": round(self.total_ms, 2),
            "overhead_ms": round(self.overhead_ms, 3),
        }
