"""Text to vectors, and optionally reranking, over HTTP.

Two kinds of embedding server are supported, chosen in the portal:

- `openai`: `POST {base_url}/embeddings`, as OpenAI and most gateways
  (LiteLLM, OpenRouter, vLLM, Ollama's /v1) serve it.
- `tei`: Hugging Face text-embeddings-inference, `POST {base_url}/embed`.

`Embedder` remembers every vector it made, in memory and in the
`embedding_cache` table, so a text is only ever sent once per model.

A reranker, if one is set up, reorders search results by reading the
question and each text together; it is slower than comparing vectors but
much better at telling which result actually answers the question.
"""

from __future__ import annotations

import array
import hashlib
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import EmbeddingSettings
from app.knowledge.breaker import Breaker
from app.integrations.executor import IntegrationError, assert_url_allowed
from app.models import EmbeddingCache

log = logging.getLogger(__name__)

Vector = list[float]

# Texts sent in one request; text-embeddings-inference refuses more than 32
# by default.
BATCH_SIZE = {"openai": 64, "tei": 32}
# Texts longer than this are cut before embedding; models read ~512 tokens.
MAX_INPUT_CHARS = 8000
# Vectors kept in memory (about 6 KB each at 1536 dimensions).
HOT_VECTORS = 2000
# The server's address is checked against the private-network guard this often.
URL_CHECK_SECONDS = 300.0
# Embedding a question is on the way to answering it, so it may not take long;
# past this the question is answered with notes found by their words.
QUERY_TIMEOUT_SECONDS = 3.0


class EmbeddingError(Exception):
    """The embedding or rerank server could not be used."""


def pack(vector: Sequence[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack(blob: bytes) -> Vector:
    values = array.array("f")
    values.frombytes(blob)
    return values.tolist()


def _cache_key(model_id: str, text: str) -> str:
    return hashlib.sha256(f"{model_id}\0{text}".encode()).hexdigest()


def model_tag(model_id: str) -> str:
    """A short, stable name for an embedding model id."""
    return hashlib.sha256(model_id.encode()).hexdigest()[:16] if model_id else ""


def _headers(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(data, dict):
        error = data.get("error") or data.get("message") or data.get("detail")
        if isinstance(error, dict):
            error = error.get("message")
        if error:
            return str(error)[:300]
    return response.text[:300]


class _UrlGuard:
    """The private-network guard integrations use, checked now and then."""

    def __init__(self) -> None:
        self._ok_until: dict[str, float] = {}

    async def check(self, url: str) -> None:
        now = time.monotonic()
        if self._ok_until.get(url, 0.0) > now:
            return
        try:
            await assert_url_allowed(url)
        except IntegrationError as exc:
            raise EmbeddingError(f"{url} is not allowed: {exc}") from exc
        self._ok_until[url] = now + URL_CHECK_SECONDS


class VectorCache:
    """Vectors by (model, text): recent ones in memory, all in SQLite."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        hot: int = HOT_VECTORS,
    ) -> None:
        self._session_factory = session_factory
        self._hot: OrderedDict[str, Vector] = OrderedDict()
        self._hot_limit = hot

    def _remember(self, key: str, vector: Vector) -> None:
        self._hot[key] = vector
        self._hot.move_to_end(key)
        while len(self._hot) > self._hot_limit:
            self._hot.popitem(last=False)

    async def get_many(self, model_id: str, texts: Sequence[str]) -> dict[str, Vector]:
        """{text: vector} for the texts already embedded with this model."""
        found: dict[str, Vector] = {}
        missing: dict[str, str] = {}
        for text in texts:
            key = _cache_key(model_id, text)
            vector = self._hot.get(key)
            if vector is not None:
                self._hot.move_to_end(key)
                found[text] = vector
            else:
                missing[key] = text
        if missing and self._session_factory is not None:
            async with self._session_factory() as db:
                rows = (
                    await db.execute(
                        select(EmbeddingCache.key, EmbeddingCache.vector).where(
                            EmbeddingCache.key.in_(list(missing))
                        )
                    )
                ).all()
            for key, blob in rows:
                vector = unpack(blob)
                self._remember(key, vector)
                found[missing[key]] = vector
        return found

    async def put_many(self, model_id: str, vectors: dict[str, Vector]) -> None:
        rows = []
        for text, vector in vectors.items():
            key = _cache_key(model_id, text)
            self._remember(key, vector)
            rows.append({"key": key, "model": model_id, "vector": pack(vector)})
        if not rows or self._session_factory is None:
            return
        try:
            async with self._session_factory() as db:
                await db.execute(sqlite_insert(EmbeddingCache).values(rows).on_conflict_do_nothing())
                await db.commit()
        except Exception:  # noqa: BLE001 - the cache is an optimisation only
            log.warning("Could not store %d vectors in the cache", len(rows), exc_info=True)

    async def drop_other_models(self, model_id: str) -> int:
        """Forget vectors of every model but this one; how many went."""
        self._hot.clear()
        if self._session_factory is None:
            return 0
        async with self._session_factory() as db:
            result = await db.execute(delete(EmbeddingCache).where(EmbeddingCache.model != model_id))
            await db.commit()
        return result.rowcount or 0


class Embedder:
    """Embeds texts with one model; built from the portal's settings."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        http: httpx.AsyncClient,
        cache: VectorCache | None = None,
        guard: _UrlGuard | None = None,
    ) -> None:
        if not settings.configured:
            raise EmbeddingError("no embedding model is set up")
        self.settings = settings
        self.model_id = settings.model_id
        # Short name of the model, stored with every vector it made.
        self.tag = model_tag(self.model_id)
        self._http = http
        self._cache = cache or VectorCache(None)
        self._guard = guard or _UrlGuard()
        # Learnt from the first answer when the settings do not say.
        self.dimensions: int | None = settings.dimensions
        self.breaker = Breaker("The embedding server")

    async def embed_query(self, text: str) -> Vector:
        timeout = min(self.settings.timeout_seconds, QUERY_TIMEOUT_SECONDS)
        return (await self._embed([self.settings.query_prefix + text], timeout))[0]

    async def embed_documents(self, texts: Sequence[str]) -> list[Vector]:
        prefix = self.settings.document_prefix
        return await self._embed([prefix + text for text in texts])

    async def _embed(self, inputs: list[str], timeout: float | None = None) -> list[Vector]:
        inputs = [text[:MAX_INPUT_CHARS] for text in inputs]
        known = await self._cache.get_many(self.model_id, inputs)
        todo = list(dict.fromkeys(text for text in inputs if text not in known))
        if todo:
            if self.breaker.open:
                raise EmbeddingError(self.breaker.reason())
            await self._guard.check(self.settings.base_url)
            size = BATCH_SIZE.get(self.settings.provider, 32)
            made: dict[str, Vector] = {}
            for start in range(0, len(todo), size):
                batch = todo[start : start + size]
                try:
                    vectors = await self._request(batch, timeout or self.settings.timeout_seconds)
                except EmbeddingError as exc:
                    self.breaker.failed(str(exc))
                    raise
                self.breaker.succeeded()
                if len(vectors) != len(batch):
                    raise EmbeddingError(
                        f"asked for {len(batch)} vectors, the server sent {len(vectors)}"
                    )
                made.update(zip(batch, vectors, strict=True))
            await self._cache.put_many(self.model_id, made)
            known.update(made)
        vectors = [known[text] for text in inputs]
        if vectors and self.dimensions is None:
            self.dimensions = len(vectors[0])
        return vectors

    async def _request(self, batch: list[str], timeout: float) -> list[Vector]:
        settings = self.settings
        base = settings.base_url.rstrip("/")
        if settings.provider == "tei":
            url, body = f"{base}/embed", {"inputs": batch, "truncate": True}
        else:
            url = f"{base}/embeddings"
            body: dict[str, Any] = {"model": settings.model, "input": batch}
            if settings.dimensions:
                body["dimensions"] = settings.dimensions
        try:
            response = await self._http.post(
                url,
                json=body,
                headers=_headers(settings.api_key),
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise EmbeddingError(f"the embedding server did not answer within {timeout:g}s") from exc
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"could not reach the embedding server: {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"the embedding server answered HTTP {response.status_code}: {_error_text(response)}"
            )
        try:
            data = response.json()
            if settings.provider == "tei":
                vectors = [[float(x) for x in row] for row in data]
            else:
                items = sorted(data["data"], key=lambda item: item.get("index", 0))
                vectors = [[float(x) for x in item["embedding"]] for item in items]
        except (ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError("the embedding server sent something that is not vectors") from exc
        return vectors


class Reranker:
    """Scores how well each text answers a question; higher is better."""

    def __init__(
        self, settings: EmbeddingSettings, http: httpx.AsyncClient, guard: _UrlGuard | None = None
    ) -> None:
        if not settings.rerank_configured:
            raise EmbeddingError("no reranker is set up")
        self.settings = settings
        self._http = http
        self._guard = guard or _UrlGuard()
        self.breaker = Breaker("The reranker")

    async def rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        """One score per text, in the order given."""
        if not texts:
            return []
        if self.breaker.open:
            raise EmbeddingError(self.breaker.reason())
        try:
            scores = await self._rerank(query, texts)
        except EmbeddingError as exc:
            self.breaker.failed(str(exc))
            raise
        self.breaker.succeeded()
        return scores

    async def _rerank(self, query: str, texts: Sequence[str]) -> list[float]:
        settings = self.settings
        await self._guard.check(settings.rerank_base_url)
        base = settings.rerank_base_url.rstrip("/")
        documents = [text[:MAX_INPUT_CHARS] for text in texts]
        if settings.rerank_provider == "tei":
            body: dict[str, Any] = {"query": query, "texts": documents, "truncate": True}
        else:
            body = {
                "model": settings.rerank_model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            }
        try:
            response = await self._http.post(
                f"{base}/rerank",
                json=body,
                headers=_headers(settings.rerank_api_key),
                timeout=min(settings.timeout_seconds, QUERY_TIMEOUT_SECONDS),
            )
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"could not reach the reranker: {exc}") from exc
        if response.status_code >= 400:
            raise EmbeddingError(
                f"the reranker answered HTTP {response.status_code}: {_error_text(response)}"
            )
        try:
            data = response.json()
            rows = data if settings.rerank_provider == "tei" else data["results"]
            scores = [float("-inf")] * len(documents)
            for row in rows:
                score = row.get("score", row.get("relevance_score"))
                scores[int(row["index"])] = float(score)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise EmbeddingError("the reranker sent something that is not scores") from exc
        return scores
