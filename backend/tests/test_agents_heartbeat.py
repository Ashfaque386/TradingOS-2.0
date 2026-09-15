"""Heartbeat tests (Build Spec §17). The critical one is structural: the
heartbeat/scheduler modules must have no importable reference to an
order-placement or risk-limit-mutation function -- not merely be denied by
a permission check, but genuinely unable to reach one.
"""

import pytest

import src.agents.heartbeat as heartbeat_module
import src.agents.scheduler as scheduler_module
from src.agents.heartbeat import DEFAULT_ELIGIBLE_AGENTS, eligible_agents, run_heartbeat
from src.gateway.apply import apply_config_text
from src.models.agent_identity import AgentIdentity
from src.models.heartbeat_log import HeartbeatStatus


def test_heartbeat_module_has_no_reference_to_mutating_functions():
    assert not hasattr(heartbeat_module, "place_order")
    assert not hasattr(heartbeat_module, "mutate_risk_limit")
    with pytest.raises(ImportError):
        from src.agents.heartbeat import place_order  # noqa: F401
    with pytest.raises(ImportError):
        from src.agents.heartbeat import mutate_risk_limit  # noqa: F401


def test_scheduler_module_also_has_no_reference_to_mutating_functions():
    assert not hasattr(scheduler_module, "place_order")
    assert not hasattr(scheduler_module, "mutate_risk_limit")
    with pytest.raises(ImportError):
        from src.agents.scheduler import place_order  # noqa: F401


def test_default_eligible_agents_fallback_when_no_gateway_config_applied(monkeypatch):
    class _EmptyState:
        def get_config(self):
            return None

    monkeypatch.setattr(heartbeat_module, "get_state", lambda: _EmptyState())

    assert eligible_agents() == DEFAULT_ELIGIBLE_AGENTS == frozenset({"ceo-agent", "risk-manager"})


CONFIG = """
{
  version: 1,
  infra: {
    llmProviders: { order: ['anthropic'] },
    brokerFailover: { primary: 'zerodha', fallback: 'upstox' },
    riskThresholdRefs: { maxDrawdownPct: 15, wsLatencyMs: 100 },
  },
  agents: {
    entries: {
      'market-analyst': { heartbeatEnabled: true },
      'ceo-agent': { heartbeatEnabled: false },
    },
  },
}
"""


async def test_eligible_agents_reads_live_heartbeat_enabled_flags(db_session_factory):
    async with db_session_factory() as db:
        await apply_config_text(db, CONFIG, source="test")

    enabled = eligible_agents()
    assert "market-analyst" in enabled
    assert "ceo-agent" not in enabled


async def test_run_heartbeat_produces_ok_log_via_granted_read_only_skill(db_session_factory):
    async with db_session_factory() as db:
        db.add(AgentIdentity(agent_id="ceo-agent", name="CEO Agent"))
        await db.commit()

    async with db_session_factory() as db:
        log = await run_heartbeat(db, "ceo-agent")

    assert log.status == HeartbeatStatus.OK
    assert "market_data" in log.details


async def test_run_heartbeat_rejects_unknown_agent(db_session_factory):
    with pytest.raises(ValueError):
        async with db_session_factory() as db:
            await run_heartbeat(db, "not-a-real-agent")
