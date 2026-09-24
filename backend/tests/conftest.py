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
import src.core.security as security
from src.api.routes.audit import get_audit_archive_root
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


@pytest.fixture(autouse=True)
def _reset_jwt_signing_key_cache():
    """`get_current_signing_key`'s in-process cache (src.core.security) is
    a module-level global, deliberately -- production only ever has one
    live process to keep in sync. Each test gets a fresh, empty database
    (see `db_session_factory` above) but that global would otherwise carry
    a stale key across tests, so a test never actually exercises the
    real "no row yet, bootstrap from settings.jwt_secret_key" path except
    the first test in the whole run. Reset before every test instead.
    """
    security.invalidate_signing_key_cache()
    yield
    security.invalidate_signing_key_cache()


@pytest.fixture(autouse=True)
def _disable_ambient_rate_limit():
    """`RateLimitMiddleware` (src.observability.rate_limit_middleware) is
    IP-keyed, and every test in this suite shares one fixed client IP via
    `httpx.ASGITransport` -- a production-sized limit would eventually
    trip partway through some unrelated test purely from this suite's own
    aggregate request volume. Set high (in effect off) for the ambient
    suite; that middleware's own dedicated tests override this fixture's
    value back down to exercise the real 429 path.
    """
    app.state.rate_limit_per_minute = 10**9
    yield
    del app.state.rate_limit_per_minute


@pytest_asyncio.fixture
async def client(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> AsyncIterator[AsyncClient]:
    # The audit archive's default path (settings.audit_archive_path) is a
    # relative "audit_archive" dir resolved off the process cwd -- the same
    # cwd a manually-run dev server uses. Without this override, any real
    # traffic sent to that dev server (e.g. a WS load smoke test's login
    # calls) lands in the exact same WORM ndjson file this suite's own
    # POST /api/v1/audit/verify tests diff the DB chain against, producing
    # a false "diverged" reading that has nothing to do with this test run.
    # A fresh per-test tmp_path keeps the archive isolated the same way
    # db_session_factory already isolates the database.
    app.dependency_overrides[get_audit_archive_root] = lambda: tmp_path / "audit_archive"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.pop(get_audit_archive_root, None)


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
