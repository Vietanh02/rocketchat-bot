"""Where vectors are kept and searched: Qdrant, over its REST API.

Only a handful of calls are needed, so this talks to Qdrant with the bot's
own HTTP client rather than through `qdrant-client`, which would bring
gRPC and numpy into a container that has little memory to spare.

Every point carries a payload; searches filter on it inside Qdrant, so a
conversation is never even shown notes it may not see. Each point also
records the embedding model that made it, and searches ask for that model
only, so vectors of an old model are ignored while they are being redone.

`MemoryVectorStore` does the same in plain Python, for tests.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.knowledge.breaker import Breaker

log = logging.getLogger(__name__)

# A search is part of answering a question; it gets less time than writes.
SEARCH_TIMEOUT_SECONDS = 3.0

# Payload fields searches filter on; Qdrant indexes them.
INDEXED_FIELDS = ("scope", "model")


class VectorStoreError(Exception):
    """The vector database could not be used."""


@dataclass(slots=True)
class Point:
    id: int
    vector: list[float]
    payload: dict[str, Any]


@dataclass(slots=True)
class Hit:
    id: int
    score: float
    payload: dict[str, Any]


# {"field": ["value", ...]}: each field must equal one of its values.
Filter = dict[str, Sequence[str]]


class VectorStore(Protocol):
    async def ensure_collection(self, name: str, dimensions: int) -> bool:
        """Create `name` for vectors of this size; True if it was (re)created.

        A collection holding vectors of another size is dropped and made anew.
        """
        ...

    async def upsert(self, name: str, points: Sequence[Point]) -> None: ...

    async def delete(self, name: str, ids: Sequence[int]) -> None: ...

    async def search(
        self,
        name: str,
        vector: Sequence[float],
        *,
        limit: int,
        where: Filter | None = None,
        min_score: float | None = None,
    ) -> list[Hit]: ...

    async def count(self, name: str) -> int | None:
        """Points in `name`; None if there is no such collection."""
        ...

    async def ping(self) -> str:
        """The server's version; raises VectorStoreError if it is unreachable."""
        ...


def _qdrant_filter(where: Filter | None) -> dict[str, Any] | None:
    if not where:
        return None
    return {
        "must": [
            {"key": key, "match": {"any": [str(value) for value in values]}}
            for key, values in where.items()
        ]
    }


class QdrantStore:
    def __init__(
        self,
        url: str,
        *,
        http: httpx.AsyncClient,
        api_key: str = "",
        prefix: str = "",
        timeout: float = 10.0,
    ) -> None:
        self._base = url.rstrip("/")
        self._http = http
        self._headers = {"api-key": api_key} if api_key else {}
        self._prefix = prefix
        self._timeout = timeout
        # Collections known to exist, with their vector size.
        self._known: dict[str, int] = {}
        self.breaker = Breaker("Qdrant")

    def _name(self, name: str) -> str:
        return f"{self._prefix}{name}"

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        missing_ok: bool = False,
        timeout: float | None = None,
    ) -> Any:
        if self.breaker.open:
            raise VectorStoreError(self.breaker.reason())
        try:
            response = await self._http.request(
                method,
                f"{self._base}{path}",
                json=body,
                headers=self._headers,
                timeout=min(self._timeout, timeout) if timeout else self._timeout,
            )
        except httpx.HTTPError as exc:
            error = str(exc) or type(exc).__name__
            self.breaker.failed(error)
            raise VectorStoreError(f"could not reach Qdrant: {error}") from exc
        self.breaker.succeeded()
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json().get("status", {}).get("error") or response.text
            except ValueError:
                detail = response.text
            raise VectorStoreError(f"Qdrant answered HTTP {response.status_code}: {detail[:300]}")
        try:
            return response.json().get("result")
        except ValueError as exc:
            raise VectorStoreError("Qdrant sent something that is not JSON") from exc

    async def ping(self) -> str:
        try:
            response = await self._http.get(
                f"{self._base}/", headers=self._headers, timeout=self._timeout
            )
            response.raise_for_status()
            version = str(response.json().get("version", "?"))
        except (httpx.HTTPError, ValueError) as exc:
            error = str(exc) or type(exc).__name__
            self.breaker.failed(error)
            raise VectorStoreError(f"could not reach Qdrant: {error}") from exc
        self.breaker.succeeded()
        return version

    async def _size(self, name: str) -> int | None:
        info = await self._call("GET", f"/collections/{self._name(name)}", missing_ok=True)
        if info is None:
            return None
        vectors = info.get("config", {}).get("params", {}).get("vectors", {})
        return vectors.get("size") if isinstance(vectors, dict) else None

    async def ensure_collection(self, name: str, dimensions: int) -> bool:
        if self._known.get(name) == dimensions:
            return False
        size = await self._size(name)
        if size == dimensions:
            self._known[name] = dimensions
            return False
        full = self._name(name)
        if size is not None:
            log.warning(
                "Qdrant collection %s holds %d-dimension vectors, the model makes %d; recreating it",
                full,
                size,
                dimensions,
            )
            await self._call("DELETE", f"/collections/{full}")
        await self._call(
            "PUT",
            f"/collections/{full}",
            {
                "vectors": {"size": dimensions, "distance": "Cosine", "on_disk": True},
                # One byte per dimension in memory instead of four.
                "quantization_config": {"scalar": {"type": "int8", "always_ram": True}},
                "on_disk_payload": True,
            },
        )
        for field in INDEXED_FIELDS:
            await self._call(
                "PUT",
                f"/collections/{full}/index?wait=true",
                {"field_name": field, "field_schema": "keyword"},
            )
        self._known[name] = dimensions
        log.info("Created Qdrant collection %s (%d dimensions)", full, dimensions)
        return True

    async def upsert(self, name: str, points: Sequence[Point]) -> None:
        if not points:
            return
        await self._call(
            "PUT",
            f"/collections/{self._name(name)}/points?wait=true",
            {
                "points": [
                    {"id": point.id, "vector": list(point.vector), "payload": point.payload}
                    for point in points
                ]
            },
        )

    async def delete(self, name: str, ids: Sequence[int]) -> None:
        if not ids:
            return
        await self._call(
            "POST",
            f"/collections/{self._name(name)}/points/delete?wait=true",
            {"points": list(ids)},
            missing_ok=True,
        )

    async def search(
        self,
        name: str,
        vector: Sequence[float],
        *,
        limit: int,
        where: Filter | None = None,
        min_score: float | None = None,
    ) -> list[Hit]:
        body: dict[str, Any] = {"query": list(vector), "limit": limit, "with_payload": True}
        if (qdrant_filter := _qdrant_filter(where)) is not None:
            body["filter"] = qdrant_filter
        if min_score is not None:
            body["score_threshold"] = min_score
        result = await self._call(
            "POST",
            f"/collections/{self._name(name)}/points/query",
            body,
            missing_ok=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
        if result is None:
            return []
        return [
            Hit(int(point["id"]), float(point["score"]), point.get("payload") or {})
            for point in result.get("points", [])
        ]

    async def count(self, name: str) -> int | None:
        result = await self._call(
            "POST",
            f"/collections/{self._name(name)}/points/count",
            {"exact": True},
            missing_ok=True,
        )
        return None if result is None else int(result.get("count", 0))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class MemoryVectorStore:
    """The same contract in plain Python; for tests and trying things out."""

    def __init__(self) -> None:
        self.collections: dict[str, tuple[int, dict[int, Point]]] = {}

    async def ping(self) -> str:
        return "memory"

    async def ensure_collection(self, name: str, dimensions: int) -> bool:
        existing = self.collections.get(name)
        if existing is not None and existing[0] == dimensions:
            return False
        self.collections[name] = (dimensions, {})
        return True

    def _points(self, name: str) -> dict[int, Point]:
        if name not in self.collections:
            raise VectorStoreError(f"no collection {name!r}")
        return self.collections[name][1]

    async def upsert(self, name: str, points: Sequence[Point]) -> None:
        stored = self._points(name)
        size = self.collections[name][0]
        for point in points:
            if len(point.vector) != size:
                raise VectorStoreError(f"vector of size {len(point.vector)}, expected {size}")
            stored[point.id] = Point(point.id, list(point.vector), dict(point.payload))

    async def delete(self, name: str, ids: Sequence[int]) -> None:
        if name in self.collections:
            for point_id in ids:
                self.collections[name][1].pop(point_id, None)

    async def search(
        self,
        name: str,
        vector: Sequence[float],
        *,
        limit: int,
        where: Filter | None = None,
        min_score: float | None = None,
    ) -> list[Hit]:
        if name not in self.collections:
            return []
        hits = []
        for point in self.collections[name][1].values():
            if where and not all(
                str(point.payload.get(key)) in {str(v) for v in values}
                for key, values in where.items()
            ):
                continue
            score = cosine(vector, point.vector)
            if min_score is None or score >= min_score:
                hits.append(Hit(point.id, score, dict(point.payload)))
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[:limit]

    async def count(self, name: str) -> int | None:
        return len(self.collections[name][1]) if name in self.collections else None
