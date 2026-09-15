from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings
from src.models import Base
from src.models.agent_to_agent_policy import AgentToAgentPolicy
from src.orchestration.handoffs import is_allowed


@pytest_asyncio.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_default_deny_with_no_rules(db_session: AsyncSession):
    assert await is_allowed(db_session, "risk-manager", "ceo-agent", "read-artefacts") is False


@pytest.mark.asyncio
async def test_explicit_allow_rule_permits_matching_request(db_session: AsyncSession):
    db_session.add(
        AgentToAgentPolicy(from_agent="risk-manager", to_agent="ceo-agent", scope="read-artefacts")
    )
    await db_session.commit()

    assert await is_allowed(db_session, "risk-manager", "ceo-agent", "read-artefacts") is True


@pytest.mark.asyncio
async def test_allow_rule_does_not_cover_other_scopes(db_session: AsyncSession):
    db_session.add(
        AgentToAgentPolicy(from_agent="risk-manager", to_agent="ceo-agent", scope="read-artefacts")
    )
    await db_session.commit()

    assert await is_allowed(db_session, "risk-manager", "ceo-agent", "write-artefacts") is False


@pytest.mark.asyncio
async def test_allow_rule_is_directional_not_symmetric(db_session: AsyncSession):
    db_session.add(
        AgentToAgentPolicy(from_agent="risk-manager", to_agent="ceo-agent", scope="read-artefacts")
    )
    await db_session.commit()

    assert await is_allowed(db_session, "ceo-agent", "risk-manager", "read-artefacts") is False


@pytest.mark.asyncio
async def test_agent_reading_its_own_artefacts_is_not_cross_agent(db_session: AsyncSession):
    # No rules at all — still allowed, since this isn't a cross-agent access.
    assert await is_allowed(db_session, "ceo-agent", "ceo-agent", "anything") is True
