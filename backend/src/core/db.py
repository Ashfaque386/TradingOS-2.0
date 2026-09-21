from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings

settings = get_settings()

# Build Spec §21-22 hardening pass: SQLAlchemy's default pool (size 5,
# overflow 10 -- 15 connections total) measured live against a real
# QueuePool exhaustion under a WebSocket fan-out load smoke test (see
# scripts/ws-fanout-load-smoke-test.mjs) -- each open WebSocket channel
# (src/api/routes/websockets.py's sign-off-queue and activity-feed) opens
# its own short-lived session on every poll interval (every 1.5-2s), so
# as few as ~30 concurrent Console connections (a handful of browser tabs
# across a small team, not an exotic load) reliably exhausted the default
# pool and made the whole app -- every HTTP route too, not just
# WebSockets -- time out until enough connections closed. Sized with
# headroom over that measured failure point, not an arbitrary bump.
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
)

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
