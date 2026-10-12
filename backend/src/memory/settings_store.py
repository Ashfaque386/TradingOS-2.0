"""Persistence for the operator's memory embedding choice (the singleton
`memory_settings` row). Kept apart from `service.py` so the service depends on
a tiny `OverrideSource` callable rather than on SQLAlchemy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.models.memory_settings import MemorySettings


@dataclass(frozen=True)
class EmbeddingChoice:
    provider: str
    model: str
    updated_by: str
    updated_at: datetime


OverrideSource = Callable[[], Awaitable[EmbeddingChoice | None]]


def _to_choice(row: MemorySettings) -> EmbeddingChoice:
    return EmbeddingChoice(
        row.embedding_provider, row.embedding_model, row.updated_by, row.updated_at
    )


async def load_choice(session: AsyncSession) -> EmbeddingChoice | None:
    row = await session.get(MemorySettings, 1)
    return _to_choice(row) if row else None


async def save_choice(
    session: AsyncSession, *, provider: str, model: str, actor: str
) -> EmbeddingChoice:
    row = await session.get(MemorySettings, 1)
    if row is None:
        row = MemorySettings(
            id=1, embedding_provider=provider, embedding_model=model, updated_by=actor
        )
        session.add(row)
    else:
        row.embedding_provider = provider
        row.embedding_model = model
        row.updated_by = actor
    await session.commit()
    await session.refresh(row)
    return _to_choice(row)


async def clear_choice(session: AsyncSession) -> bool:
    result = await session.execute(delete(MemorySettings).where(MemorySettings.id == 1))
    await session.commit()
    return bool(getattr(result, "rowcount", 0))


def source_from_factory(factory: async_sessionmaker[AsyncSession]) -> OverrideSource:
    async def source() -> EmbeddingChoice | None:
        async with factory() as session:
            return await load_choice(session)

    return source
