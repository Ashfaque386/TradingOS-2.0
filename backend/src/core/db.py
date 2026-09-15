from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, pool_pre_ping=True)

AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """For code that needs to open more than one session itself (the task
    engine's concurrent dispatch — see src/orchestration/task_engine.py),
    not a single request-scoped session. A FastAPI dependency (like
    get_db) so tests can override it with their own isolated
    engine/session_factory the same way they override get_db — see
    tests/conftest.py's db_session_factory fixture.
    """
    return AsyncSessionLocal
