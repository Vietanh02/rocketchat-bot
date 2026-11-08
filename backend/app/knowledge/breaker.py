"""Stop calling a server for a while after it failed.

When the embedding server or Qdrant is down or hanging, every question
would otherwise wait for it to time out before being answered from the
words alone. After a failure the server is left alone for a short pause,
and calls fail at once; the first call after the pause tries it again.
"""

from __future__ import annotations

import time

PAUSE_SECONDS = 30.0


class Breaker:
    def __init__(self, what: str, pause: float = PAUSE_SECONDS) -> None:
        self.what = what
        self.pause = pause
        self._open_until = 0.0
        self.last_error = ""

    @property
    def open(self) -> bool:
        """True while calls should not be tried."""
        return time.monotonic() < self._open_until

    def failed(self, error: str) -> None:
        self._open_until = time.monotonic() + self.pause
        self.last_error = error

    def succeeded(self) -> None:
        self._open_until = 0.0
        self.last_error = ""

    def reason(self) -> str:
        left = max(0.0, self._open_until - time.monotonic())
        return f"{self.what} failed recently ({self.last_error}); retrying in {left:.0f}s"
