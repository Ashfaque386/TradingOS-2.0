"""Heartbeat (Build Spec §17): a configurable-interval, read-only
self-check for eligible agents (CEO, Risk Manager by default -- any agent
with heartbeat_enabled=true in its effective Gateway config, reusing
Phase 1's existing AgentDefaultsConfig.heartbeat_enabled /
AgentEntryConfig.heartbeat_enabled overrides and
heartbeat_interval_minutes).

Structural isolation (the critical part of this requirement): this module
never imports anything from src.execution or src.risk -- the modules that
hold (future) order-placement and risk-limit-mutation code. That is not a
convention to remember, it's the literal Python import graph: no name in
this module's globals resolves to place_order or mutate_risk_limit, so
calling either from here isn't merely denied by a permission check, it's a
plain NameError -- those functions are not in scope to call.
tests/test_agents_heartbeat.py asserts this by trying to import them from
this module and confirming the import fails, not just that calling them
raises PermissionError.

The self-check itself only ever goes through
src.agents.tools.registry.execute_skill() -- the same grant-gated call path
everything else uses -- restricted to read-only skills (market-data-read).
"""

import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.tools.registry import execute_skill
from src.gateway.roster import ROSTER_BY_ID
from src.gateway.state import get_state
from src.models.heartbeat_log import HeartbeatLog, HeartbeatStatus

logger = structlog.get_logger(__name__)

# Build Spec §17: eligible by default. Only used as a fallback when no
# Gateway config has been applied yet -- once one is, eligible_agents()
# below reads the real per-agent heartbeat_enabled flags instead.
DEFAULT_ELIGIBLE_AGENTS: frozenset[str] = frozenset({"ceo-agent", "risk-manager"})


async def run_heartbeat(db: AsyncSession, agent_id: str) -> HeartbeatLog:
    """One read-only self-check for one agent: can it still reach its
    granted read-only data skill? A failure here (skill not granted, skill
    execution error) is itself what "raise an alert" means for a heartbeat
    -- there's nothing to place an order or change a risk limit with, even
    on failure.
    """
    if agent_id not in ROSTER_BY_ID:
        raise ValueError(f"unknown agent id: {agent_id!r}")

    details: dict = {"checked_at": time.time()}
    status = HeartbeatStatus.OK
    try:
        details["market_data"] = await execute_skill(agent_id, "market-data-read")
    except Exception as exc:  # noqa: BLE001 - any self-check failure IS the alert
        status = HeartbeatStatus.ALERT
        details["error"] = str(exc)

    log = HeartbeatLog(agent_id=agent_id, status=status, details=details)
    db.add(log)
    await db.commit()
    await db.refresh(log)
    if status == HeartbeatStatus.ALERT:
        logger.warning("agents.heartbeat.alert", agent_id=agent_id, details=details)
    return log


def eligible_agents() -> frozenset[str]:
    config = get_state().get_config()
    if config is None:
        return DEFAULT_ELIGIBLE_AGENTS
    effective = get_state().get_all_effective_agents()
    enabled = frozenset(
        agent_id for agent_id, eff in effective.items() if eff.heartbeat_enabled and eff.enabled
    )
    return enabled or DEFAULT_ELIGIBLE_AGENTS
