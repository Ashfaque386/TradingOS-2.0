"""Heartbeat (Build Spec §17): a configurable-interval, read-only
self-check for eligible agents (CEO, Risk Manager by default -- any agent
with heartbeat_enabled=true in its effective Gateway config, reusing
Phase 1's existing AgentDefaultsConfig.heartbeat_enabled /
AgentEntryConfig.heartbeat_enabled overrides and
heartbeat_interval_minutes).

Structural isolation (the critical part of this requirement): importing
this module must not load any module that can place an order or change a
risk limit -- src.orchestration (live_trading, paper_trading, risk_gate,
risk_limits, kill_switch ...), src.brokers or src.engine. That is not a
convention to remember, it's the literal Python import graph: nothing that
can reach those functions is in scope here, so calling one from here isn't
merely denied by a permission check, it's a plain NameError.
tests/test_agents_heartbeat.py asserts this by importing this module (and
the scheduler) in a fresh interpreter and listing what ended up in
sys.modules, so it checks the real closure, including what a dependency
pulls in, not just the names in this file's globals.

The self-check itself only ever goes through
src.agents.tools.registry.execute_skill() -- the same grant-gated call path
everything else uses -- restricted to read-only skills (market-data-read).
"""

import time
from typing import Any

import structlog
from sqlalchemy import delete, select
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

# Seven days of checks at the default one every five minutes.
HEARTBEAT_LOGS_KEPT_PER_AGENT = 7 * 24 * 12


async def run_heartbeat(db: AsyncSession, agent_id: str) -> HeartbeatLog:
    """One read-only self-check for one agent: can it still reach its
    granted read-only data skill? A failure here (skill not granted, skill
    execution error) is itself what "raise an alert" means for a heartbeat
    -- there's nothing to place an order or change a risk limit with, even
    on failure.
    """
    if agent_id not in ROSTER_BY_ID:
        raise ValueError(f"unknown agent id: {agent_id!r}")

    details: dict[str, Any] = {"checked_at": time.time()}
    status = HeartbeatStatus.OK
    try:
        details["market_data"] = await execute_skill(agent_id, "market-data-read")
    except Exception as exc:  # noqa: BLE001 - any self-check failure IS the alert
        status = HeartbeatStatus.ALERT
        details["error"] = str(exc)

    log = HeartbeatLog(agent_id=agent_id, status=status, details=details)
    db.add(log)
    await db.flush()
    await _prune_old_logs(db, agent_id)
    await db.commit()
    await db.refresh(log)
    if status == HeartbeatStatus.ALERT:
        logger.warning("agents.heartbeat.alert", agent_id=agent_id, details=details)
    return log


async def _prune_old_logs(db: AsyncSession, agent_id: str) -> None:
    """Keeps the newest HEARTBEAT_LOGS_KEPT_PER_AGENT rows for this agent (the console shows the
    last 20). Until 2026-11-21 nothing pruned the table: about 576 rows a day for the two default
    agents, kept for ever. Same keep-N shape as src/observability/scheduled_job_history.py."""
    stale_ids = (
        (
            await db.execute(
                select(HeartbeatLog.id)
                .where(HeartbeatLog.agent_id == agent_id)
                .order_by(HeartbeatLog.checked_at.desc(), HeartbeatLog.id.desc())
                .offset(HEARTBEAT_LOGS_KEPT_PER_AGENT)
            )
        )
        .scalars()
        .all()
    )
    if stale_ids:
        await db.execute(delete(HeartbeatLog).where(HeartbeatLog.id.in_(stale_ids)))


def eligible_agents() -> frozenset[str]:
    config = get_state().get_config()
    if config is None:
        return DEFAULT_ELIGIBLE_AGENTS
    effective = get_state().get_all_effective_agents()
    enabled = frozenset(
        agent_id for agent_id, eff in effective.items() if eff.heartbeat_enabled and eff.enabled
    )
    return enabled or DEFAULT_ELIGIBLE_AGENTS
