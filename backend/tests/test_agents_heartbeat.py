"""Heartbeat tests (Build Spec §17). The critical one is structural: the
heartbeat/scheduler modules must not be able to reach an order-placement or
risk-limit-mutation function -- not merely be denied by a permission check,
but genuinely unable to load the code that holds one.
"""

import json
import subprocess  # nosec B404 -- runs this interpreter on a fixed snippet, see _loaded_src_modules
import sys
from pathlib import Path

import pytest

import src.agents.heartbeat as heartbeat_module
from src.agents.heartbeat import DEFAULT_ELIGIBLE_AGENTS, eligible_agents, run_heartbeat
from src.gateway.apply import apply_config_text
from src.models.agent_identity import AgentIdentity
from src.models.heartbeat_log import HeartbeatStatus

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Where an order is placed, a risk limit is changed, or a switch is tripped. Nothing the
# heartbeat imports, directly or through a dependency, may load any of them.
FORBIDDEN_PREFIXES = ("src.orchestration", "src.brokers", "src.engine")
FORBIDDEN_MODULES = (
    "src.orchestration.live_trading",
    "src.orchestration.paper_trading",
    "src.orchestration.risk_gate",
    "src.orchestration.risk_limits",
    "src.orchestration.kill_switch",
)


def _loaded_src_modules(*imports: str) -> set[str]:
    """The src.* modules a fresh interpreter has loaded after importing `imports`.

    A fresh interpreter, because this process has long since imported everything: only a
    clean one shows what these modules themselves pull in.
    """
    code = (
        "import json, sys; "
        + "; ".join(f"import {name}" for name in imports)
        + "; print(json.dumps(sorted(m for m in sys.modules if m.startswith('src.'))))"
    )
    result = subprocess.run(  # nosec B603 -- sys.executable and a snippet built from constants
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=BACKEND_DIR,
        timeout=120,
        check=True,
    )
    return set(json.loads(result.stdout.strip().splitlines()[-1]))


def test_the_import_probe_can_see_a_forbidden_module():
    """The positive control: without it the isolation tests below would pass on a probe that
    sees nothing."""
    loaded = _loaded_src_modules("src.orchestration.live_trading")

    assert "src.orchestration.live_trading" in loaded
    assert any(m.startswith(FORBIDDEN_PREFIXES) for m in loaded)


def test_heartbeat_and_scheduler_load_nothing_that_places_orders_or_changes_risk_limits():
    loaded = _loaded_src_modules("src.agents.heartbeat", "src.agents.scheduler")

    assert "src.agents.heartbeat" in loaded
    assert "src.agents.scheduler" in loaded
    offenders = sorted(m for m in loaded if m.startswith(FORBIDDEN_PREFIXES))
    assert offenders == [], f"the heartbeat can reach order or risk code through: {offenders}"
    for module in FORBIDDEN_MODULES:
        assert module not in loaded


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
