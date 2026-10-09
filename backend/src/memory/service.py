"""Agent long-term memory: the one object the API and the LangGraph pipeline
both talk to. It owns the embedding provider and the Qdrant store, and is the
single place that decides whether memory is *available* -- which it only is
when a Qdrant server is configured and reachable AND a real embedding provider
resolves. Otherwise every operation raises `MemoryUnavailableError` carrying a
reason an operator can act on; nothing is faked.

The pipeline never sees this class. `src.agents.graph.AgentMemory` is a
Protocol (the graph stays infra-independent, same pattern as
`PipelineEventSink`), and `PipelineMemory` below is the adapter that satisfies
it, swallowing every failure -- memory is an aid to a run, never a reason for
one to fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.core.config import Settings, get_settings
from src.memory.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    build_embedding_provider,
)
from src.memory.store import (
    MEMORY_KINDS,
    CollectionStats,
    MemoryHit,
    MemoryStoreError,
    QdrantMemoryStore,
    new_memory_id,
)

logger = structlog.get_logger(__name__)

MAX_TEXT_CHARS = 4000
MAX_QUERY_LIMIT = 20
_PIPELINE_KIND = "strategy"
_PROBE_TTL_SECONDS = 60.0


class MemoryUnavailableError(Exception):
    """Memory is not configured or not reachable; the message says why."""


class MemoryValidationError(Exception):
    """The caller's input was unusable (unknown kind, empty or oversize text)."""


@dataclass(frozen=True)
class MemoryStatus:
    available: bool
    reason: str | None
    qdrant_configured: bool
    qdrant_reachable: bool
    embedding_provider: str | None
    embedding_model: str | None
    collections: list[CollectionStats] = field(default_factory=list)


def _check_kind(kind: str) -> None:
    if kind not in MEMORY_KINDS:
        raise MemoryValidationError(
            f"unknown memory kind {kind!r} (one of: {', '.join(MEMORY_KINDS)})"
        )


def _check_text(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise MemoryValidationError("text must not be empty")
    if len(cleaned) > MAX_TEXT_CHARS:
        raise MemoryValidationError(
            f"text is {len(cleaned)} characters; the limit is {MAX_TEXT_CHARS}"
        )
    return cleaned


class MemoryService:
    def __init__(
        self,
        settings: Settings,
        *,
        store: QdrantMemoryStore | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._embedder = embedder
        self._injected = store is not None or embedder is not None
        self._probe_cache: dict[tuple[str, str], tuple[float, str | None]] = {}

    # -- resolution --------------------------------------------------------

    def _resolve_store(self) -> QdrantMemoryStore:
        if self._store is not None:
            return self._store
        if not self._settings.qdrant_url.strip():
            raise MemoryUnavailableError("QDRANT_URL is not set")
        self._store = QdrantMemoryStore.from_url(
            self._settings.qdrant_url.strip(), self._settings.qdrant_api_key
        )
        return self._store

    def _resolve_embedder(self) -> EmbeddingProvider:
        # Rebuilt on every call (cheap: it only reads settings and the
        # credential store) unless one was injected, so adding an API key or
        # changing the provider in Settings takes effect without a restart.
        if self._embedder is not None:
            return self._embedder
        provider, reason = build_embedding_provider(self._settings)
        if provider is None:
            raise MemoryUnavailableError(reason or "no embedding provider")
        return provider

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return await self._resolve_embedder().embed(texts)
        except EmbeddingError as exc:
            raise MemoryUnavailableError(f"embedding failed: {exc}") from exc

    async def aclose(self) -> None:
        if self._store is not None and not self._injected:
            await self._store.close()
            self._store = None

    # -- status ------------------------------------------------------------

    async def _probe_embedder(self, embedder: EmbeddingProvider) -> str | None:
        """Resolving a provider only proves it is *configured*; whether the
        model actually embeds (an Ollama chat model answers 501, a mistyped
        model name 404s) is only known by asking it. One tiny request, cached
        for a minute so a panel refresh doesn't hammer a paid API."""
        key = (embedder.name, embedder.model)
        now = time.monotonic()
        cached = self._probe_cache.get(key)
        if cached is not None and now - cached[0] < _PROBE_TTL_SECONDS:
            return cached[1]
        try:
            await embedder.embed(["status check"])
            error = None
        except EmbeddingError as exc:
            error = f"embedding failed: {exc}"
        self._probe_cache[key] = (now, error)
        return error

    async def status(self) -> MemoryStatus:
        configured = bool(self._settings.qdrant_url.strip()) or self._store is not None
        provider_name: str | None = None
        model: str | None = None
        reasons: list[str] = []

        try:
            embedder = self._resolve_embedder()
            provider_name, model = embedder.name, embedder.model
            probe_error = await self._probe_embedder(embedder)
            if probe_error:
                reasons.append(probe_error)
        except MemoryUnavailableError as exc:
            reasons.append(str(exc))

        reachable = False
        collections: list[CollectionStats] = []
        if not configured:
            reasons.insert(0, "QDRANT_URL is not set")
        else:
            try:
                store = self._resolve_store()
                await store.ping()
                reachable = True
                collections = await store.stats()
            except (MemoryUnavailableError, MemoryStoreError) as exc:
                reasons.insert(0, f"vector store: {exc}")
            except Exception as exc:  # noqa: BLE001 - qdrant-client raises its own/httpx types
                reasons.insert(0, f"vector store unreachable: {type(exc).__name__}: {exc}")

        return MemoryStatus(
            available=not reasons,
            reason="; ".join(reasons) or None,
            qdrant_configured=configured,
            qdrant_reachable=reachable,
            embedding_provider=provider_name,
            embedding_model=model,
            collections=collections,
        )

    # -- operations --------------------------------------------------------

    async def remember(self, kind: str, text: str, metadata: dict[str, Any] | None = None) -> str:
        _check_kind(kind)
        cleaned = _check_text(text)
        store = self._resolve_store()
        (vector,) = await self._embed([cleaned])
        memory_id = new_memory_id()
        try:
            await store.upsert(
                kind,
                memory_id=memory_id,
                vector=vector,
                text=cleaned,
                payload={**(metadata or {}), "created_at": time.time()},
            )
        except MemoryStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailableError(f"vector store: {type(exc).__name__}: {exc}") from exc
        return memory_id

    async def query(
        self, text: str, *, kind: str | None = None, limit: int = 5
    ) -> list[tuple[str, MemoryHit]]:
        """Similarity search; `kind=None` searches every collection and merges
        by score. Returns (kind, hit) pairs, best first."""
        if kind is not None:
            _check_kind(kind)
        cleaned = _check_text(text)
        limit = max(1, min(limit, MAX_QUERY_LIMIT))
        store = self._resolve_store()
        (vector,) = await self._embed([cleaned])
        kinds = (kind,) if kind else MEMORY_KINDS
        merged: list[tuple[str, MemoryHit]] = []
        try:
            for k in kinds:
                merged.extend((k, hit) for hit in await store.search(k, vector, limit))
        except MemoryStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailableError(f"vector store: {type(exc).__name__}: {exc}") from exc
        merged.sort(key=lambda pair: pair[1].score, reverse=True)
        return merged[:limit]

    async def forget(self, kind: str, memory_id: str) -> bool:
        _check_kind(kind)
        store = self._resolve_store()
        try:
            return await store.delete(kind, memory_id)
        except MemoryStoreError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MemoryUnavailableError(f"vector store: {type(exc).__name__}: {exc}") from exc


class PipelineMemory:
    """`src.agents.graph.AgentMemory` over a `MemoryService`: what a pipeline
    run recalls before it starts and records as it learns. Every method
    swallows failure -- a run proceeds with less context rather than failing."""

    def __init__(self, service: MemoryService, *, recall_limit: int) -> None:
        self._service = service
        self._recall_limit = recall_limit

    async def recall(self, objective: str) -> list[str]:
        if self._recall_limit <= 0:
            return []
        try:
            hits = await self._service.query(
                objective, kind=_PIPELINE_KIND, limit=self._recall_limit
            )
        except Exception as exc:  # noqa: BLE001 - see class docstring
            logger.info("memory.recall_skipped", reason=str(exc)[:200])
            return []
        return [hit.text for _, hit in hits]

    async def remember(self, text: str, metadata: dict[str, Any]) -> None:
        try:
            await self._service.remember(_PIPELINE_KIND, text[:MAX_TEXT_CHARS], metadata)
        except Exception as exc:  # noqa: BLE001 - see class docstring
            logger.info("memory.remember_skipped", reason=str(exc)[:200])


_service: MemoryService | None = None


def get_memory_service() -> MemoryService:
    """Process-wide service, so there is one Qdrant client. Used as a FastAPI
    dependency, which tests override with `app.dependency_overrides`."""
    global _service
    if _service is None:
        _service = MemoryService(get_settings())
    return _service


def get_pipeline_memory() -> PipelineMemory:
    return PipelineMemory(get_memory_service(), recall_limit=get_settings().memory_recall_limit)
