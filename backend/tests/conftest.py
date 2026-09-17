import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos_test"
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-do-not-use-in-production")

from collections.abc import AsyncIterator, Callable, Coroutine

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.core.db as db_module
from src.core.config import get_settings
from src.core.roles import Role
from src.core.security import hash_password
from src.main import app
from src.models import Base
from src.models.user import User


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh engine + fresh schema per test, wired into the app via a
    dependency override. A new engine (rather than reusing src.core.db's
    module-level singleton) keeps every test's connections on that test's
    own event loop.
    """
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[db_module.get_db] = override_get_db
    # Orchestration routes/task-engine dispatch need to open more than one
    # session themselves (concurrent task execution) — see
    # src/orchestration/task_engine.py's module docstring — so they depend
    # on get_session_factory rather than a single get_db session. Override
    # it the same way, to the same isolated per-test engine.
    app.dependency_overrides[db_module.get_session_factory] = lambda: session_factory
    # src.observability.audit_middleware.AuditLoggingMiddleware is pure
    # ASGI middleware, outside the dependency-injection graph entirely, so
    # dependency_overrides can't reach it — it reads app.state directly.
    previous_session_factory = app.state.db_session_factory
    app.state.db_session_factory = session_factory
    yield session_factory
    app.dependency_overrides.pop(db_module.get_db, None)
    app.dependency_overrides.pop(db_module.get_session_factory, None)
    app.state.db_session_factory = previous_session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator[Redis]:
    """A real Redis client (this sandbox/CI runs a real redis-server) for
    orchestration tests exercising the event bus's publish side directly.
    Pub/sub has no durable state to reset between tests, so no cleanup
    beyond closing the connection is needed.
    """
    settings = get_settings()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def client(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def make_user(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> Callable[[str, str, Role], Coroutine[None, None, User]]:
    """Insert a user directly, bypassing the public /auth/register endpoint —
    needed for RBAC tests, since registration can only ever produce the
    bootstrap SystemAdministrator or a ReadOnlyAuditor (see auth.register).
    """

    async def _make(email: str, password: str, role: Role) -> User:
        async with db_session_factory() as session:
            user = User(email=email, hashed_password=hash_password(password), role=role)
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    return _make
