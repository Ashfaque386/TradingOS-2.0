"""Qdrant-backed vector store for agent memory: one collection per memory kind
(`strategy`, `news`, `organization`, the split the old app used), cosine
distance, created lazily on first write once the embedding dimension is known.

The store never embeds anything -- it is handed vectors -- so it has no opinion
about providers, and tests can drive it with `AsyncQdrantClient(":memory:")`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qm

MEMORY_KINDS = ("strategy", "news", "organization")
_COLLECTION_PREFIX = "tradingos_memory_"


class MemoryStoreError(Exception):
    """The vector store rejected or could not complete an operation."""


class DimensionMismatchError(MemoryStoreError):
    """The collection was created for a different embedding dimension than the
    configured model produces (the model was changed). Vectors of different
    sizes cannot share a collection, and silently re-creating it would destroy
    what the agents have learned -- so this is reported, not resolved."""


@dataclass(frozen=True)
class MemoryHit:
    id: str
    score: float
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CollectionStats:
    kind: str
    exists: bool
    count: int
    dimension: int | None


def collection_name(kind: str) -> str:
    return f"{_COLLECTION_PREFIX}{kind}"


def new_memory_id() -> str:
    return str(uuid.uuid4())


class QdrantMemoryStore:
    def __init__(self, client: AsyncQdrantClient) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, api_key: str | None = None) -> QdrantMemoryStore:
        return cls(AsyncQdrantClient(url=url, api_key=api_key, timeout=10))

    async def close(self) -> None:
        await self._client.close()

    async def _dimension(self, name: str) -> int | None:
        """The collection's vector size, or None if it does not exist."""
        if not await self._client.collection_exists(name):
            return None
        info = await self._client.get_collection(name)
        vectors = info.config.params.vectors
        if isinstance(vectors, qm.VectorParams):
            return int(vectors.size)
        raise MemoryStoreError(f"{name}: unexpected named-vector collection layout")

    async def ensure_collection(self, kind: str, dimension: int) -> None:
        name = collection_name(kind)
        existing = await self._dimension(name)
        if existing is None:
            await self._client.create_collection(
                name,
                vectors_config=qm.VectorParams(size=dimension, distance=qm.Distance.COSINE),
            )
            return
        if existing != dimension:
            raise DimensionMismatchError(
                f"{kind} memory holds {existing}-dimension vectors but the configured "
                f"embedding model produces {dimension}. Switch back to the original "
                "model, or clear this memory collection to start over."
            )

    async def upsert(
        self,
        kind: str,
        *,
        memory_id: str,
        vector: list[float],
        text: str,
        payload: dict[str, Any],
    ) -> None:
        await self.ensure_collection(kind, len(vector))
        await self._client.upsert(
            collection_name(kind),
            points=[qm.PointStruct(id=memory_id, vector=vector, payload={**payload, "text": text})],
        )

    async def search(self, kind: str, vector: list[float], limit: int) -> list[MemoryHit]:
        name = collection_name(kind)
        existing = await self._dimension(name)
        if existing is None:
            return []  # nothing written yet is an empty result, not an error
        if existing != len(vector):
            raise DimensionMismatchError(
                f"{kind} memory holds {existing}-dimension vectors but the query "
                f"embedding has {len(vector)}"
            )
        result = await self._client.query_points(name, query=vector, limit=limit, with_payload=True)
        hits: list[MemoryHit] = []
        for point in result.points:
            payload = dict(point.payload or {})
            text = str(payload.pop("text", ""))
            hits.append(
                MemoryHit(id=str(point.id), score=float(point.score), text=text, payload=payload)
            )
        return hits

    async def delete(self, kind: str, memory_id: str) -> bool:
        """Delete one memory; False if it (or the collection) did not exist."""
        name = collection_name(kind)
        if await self._dimension(name) is None:
            return False
        found = await self._client.retrieve(name, ids=[memory_id], with_payload=False)
        if not found:
            return False
        await self._client.delete(name, points_selector=qm.PointIdsList(points=[memory_id]))
        return True

    async def stats(self) -> list[CollectionStats]:
        result: list[CollectionStats] = []
        for kind in MEMORY_KINDS:
            name = collection_name(kind)
            dimension = await self._dimension(name)
            if dimension is None:
                result.append(CollectionStats(kind=kind, exists=False, count=0, dimension=None))
                continue
            counted = await self._client.count(name, exact=True)
            result.append(
                CollectionStats(kind=kind, exists=True, count=counted.count, dimension=dimension)
            )
        return result

    async def ping(self) -> None:
        """Raises if the server cannot be reached."""
        await self._client.get_collections()
