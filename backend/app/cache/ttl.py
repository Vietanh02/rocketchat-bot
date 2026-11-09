"""A least-recently-used cache whose entries expire."""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Generic, TypeVar

V = TypeVar("V")


class TTLCache(Generic[V]):
    """At most `size` entries, each kept `ttl` seconds unless `put` says otherwise."""

    def __init__(self, size: int, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.size = size
        self.ttl = ttl
        self._clock = clock
        self._items: OrderedDict[Hashable, tuple[float, V]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._items)

    def get(self, key: Hashable) -> V | None:
        item = self._items.get(key)
        if item is None:
            self.misses += 1
            return None
        expires, value = item
        if expires <= self._clock():
            del self._items[key]
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return value

    def pop(self, key: Hashable) -> V | None:
        """Take an entry out; None if there is none or it expired."""
        item = self._items.pop(key, None)
        if item is None or item[0] <= self._clock():
            return None
        return item[1]

    def put(self, key: Hashable, value: V, ttl: float | None = None) -> None:
        seconds = self.ttl if ttl is None else ttl
        if seconds <= 0 or self.size <= 0:
            return
        self._items[key] = (self._clock() + seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def stats(self) -> dict[str, int]:
        return {"entries": len(self._items), "hits": self.hits, "misses": self.misses}
