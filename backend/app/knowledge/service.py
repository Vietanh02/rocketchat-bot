"""The embedding model, reranker and vector database, as the bot sees them.

The vector database comes from the environment (`APP_QDRANT_URL`); the
embedding model and reranker from the portal. Either may be missing, and
then searches fall back to matching words only: `embedder_for` and
`reranker_for` return None, and `store` is None.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config_store import EmbeddingSettings
from app.config import get_settings
from app.net import httpx_verify
from app.knowledge.embeddings import Embedder, Reranker, VectorCache, _UrlGuard
from app.knowledge.vectorstore import QdrantStore, VectorStore

log = logging.getLogger(__name__)


class KnowledgeService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None,
        *,
        http_client: httpx.AsyncClient | None = None,
        store: VectorStore | None = None,
        use_env_store: bool = True,
    ) -> None:
        # Tests hand in a client with a fake transport and an in-memory store.
        self._http = http_client
        self._own_http = http_client is None
        self.cache = VectorCache(session_factory)
        self._guard = _UrlGuard()
        self._embedder: Embedder | None = None
        self._reranker: Reranker | None = None
        self._built_from: EmbeddingSettings | None = None
        self.store = store
        if store is None and use_env_store:
            settings = get_settings()
            if settings.qdrant_url:
                self.store = QdrantStore(
                    settings.qdrant_url,
                    http=self.http,
                    api_key=settings.qdrant_api_key,
                    prefix=settings.qdrant_collection_prefix,
                    timeout=settings.qdrant_timeout_seconds,
                )

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                timeout=30.0,
                verify=httpx_verify(),
            )
        return self._http

    def _rebuild(self, settings: EmbeddingSettings) -> None:
        if settings == self._built_from:
            return
        self._embedder = (
            Embedder(settings, self.http, self.cache, self._guard) if settings.configured else None
        )
        self._reranker = (
            Reranker(settings, self.http, self._guard) if settings.rerank_configured else None
        )
        self._built_from = settings

    def embedder_for(self, settings: EmbeddingSettings) -> Embedder | None:
        self._rebuild(settings)
        return self._embedder

    def reranker_for(self, settings: EmbeddingSettings) -> Reranker | None:
        self._rebuild(settings)
        return self._reranker

    def vectors_for(self, settings: EmbeddingSettings) -> tuple[Embedder, VectorStore] | None:
        """The embedder and store together, when both are available."""
        embedder = self.embedder_for(settings)
        if embedder is None or self.store is None:
            return None
        return embedder, self.store

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
            self._http = None
