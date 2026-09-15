import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import get_settings
from src.gateway.apply import apply_config_from_file
from src.gateway.state import get_state
from src.gateway.watcher import ConfigWatcher
from src.models import Base
from src.models.agent_config_version import AgentConfigVersion, ConfigVersionStatus

VALID_CONFIG_JSON5 = """
{{
  version: 1,
  infra: {{
    llmProviders: {{ order: ['anthropic'] }},
    brokerFailover: {{ primary: 'zerodha', fallback: 'upstox' }},
    riskThresholdRefs: {{ maxDrawdownPct: 15, wsLatencyMs: 100 }},
  }},
  agents: {{
    entries: {{
      'ceo-agent': {{
        identity: {{ name: 'CEO', emoji: '🧠', theme: '{theme}' }},
      }},
    }},
  }},
}}
"""


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    yield factory
    await engine.dispose()


async def _poll_until(
    predicate, *, timeout_seconds: float = 5.0, interval_seconds: float = 0.1
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval_seconds)
    return False


@pytest.mark.asyncio
async def test_hot_reload_applies_valid_change_and_rejects_invalid_keeping_last_known_good(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
):
    config_path = tmp_path / "tradingos.config.json"
    config_path.write_text(VALID_CONFIG_JSON5.format(theme="cyan"))

    async with session_factory() as session:
        seed_result = await apply_config_from_file(session, config_path, source="test-seed")
    assert seed_result.status == ConfigVersionStatus.ACTIVE

    watcher = ConfigWatcher(config_path, source="test-hot-reload")
    watcher.start()
    try:
        # --- a valid hand-edit applies within a few seconds ---
        config_path.write_text(VALID_CONFIG_JSON5.format(theme="magenta"))

        def _theme_is_magenta() -> bool:
            agent = get_state().get_effective_agent("ceo-agent")
            return agent is not None and agent.theme == "magenta"

        applied = await _poll_until(_theme_is_magenta)
        assert applied, "valid hot-reload change did not apply within the timeout"
        version_after_valid_change = get_state().get_version_id()

        # --- an invalid hand-edit is rejected, with a clear error, and the
        # app keeps running on the last-known-good (still "magenta") config ---
        config_path.write_text("{ this is not valid json5 !!! ")

        async def _rejected_row_exists() -> bool:
            async with session_factory() as session:
                result = await session.execute(
                    select(AgentConfigVersion).where(
                        AgentConfigVersion.status == ConfigVersionStatus.REJECTED
                    )
                )
                return result.scalars().first() is not None

        deadline = time.monotonic() + 5.0
        rejected_seen = False
        while time.monotonic() < deadline:
            if await _rejected_row_exists():
                rejected_seen = True
                break
            await asyncio.sleep(0.1)

        assert rejected_seen, "invalid hot-reload change was never recorded as rejected"

        # State must be untouched by the rejected change.
        assert get_state().get_version_id() == version_after_valid_change
        assert get_state().get_effective_agent("ceo-agent").theme == "magenta"

        async with session_factory() as session:
            rejected_row = (
                (
                    await session.execute(
                        select(AgentConfigVersion).where(
                            AgentConfigVersion.status == ConfigVersionStatus.REJECTED
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert rejected_row.validation_errors  # a clear error, not a silent failure
    finally:
        watcher.stop()
