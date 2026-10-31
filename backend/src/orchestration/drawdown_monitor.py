"""The automatic half of the Max Drawdown Kill Switch (Phase 3).

Until now nothing watched equity: the switch tripped only when someone posted equity and peak to
`POST /kill-switch/{mode}/check`. `evaluate_mode_drawdown` takes a measured reading
(src.orchestration.equity), stores it, works out the drawdown from the highest equity recorded
since the last human reset, and trips the switch when it reaches the limit.

Three rules, each load-bearing:

* **The limit is the dual-control one.** The threshold is read from the applied
  `max_drawdown_pct` risk limit (`get_effective_risk_limit`), so a change two people agreed to
  actually takes effect; nothing a request body says is stored as the limit.
* **A gap is not a loss.** A reading that could not be measured (an unpriced position, no
  starting capital) is stored as such and evaluated as nothing: it neither trips the switch nor
  clears a drawdown.
* **Latched, and accountable.** Tripping is the only thing this does to the switch; clearing it
  stays a human act (`reset_kill_switch`). An automatic trip writes its own audit row, because the
  audit middleware only records HTTP requests and this is not one.

The state row is locked for the whole evaluation, so the scheduler and an operator's "evaluate
now" cannot both read an untripped switch and both trip it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.engine.risk.kill_switch import DEFAULT_MAX_DRAWDOWN_PCT, evaluate_drawdown
from src.models.kill_switch_state import KillSwitchState
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel
from src.orchestration.equity import EquitySample, peak_equity_since, record_snapshot
from src.orchestration.kill_switch import get_kill_switch_state
from src.orchestration.risk_limit_catalog import MAX_DRAWDOWN_LIMIT
from src.orchestration.risk_limits import get_effective_risk_limit

__all__ = ["MONITOR_ACTOR", "MonitorOutcome", "evaluate_mode_drawdown"]

MONITOR_ACTOR = "system:drawdown-monitor"


@dataclass(frozen=True, slots=True)
class MonitorOutcome:
    # ok: measured, below the limit; tripped: this reading tripped the switch; already_tripped:
    # measured while the switch was latched; cannot_evaluate: the reading could not be measured.
    status: Literal["ok", "tripped", "already_tripped", "cannot_evaluate"]
    sample: EquitySample
    peak_equity: float | None
    drawdown_pct: float | None
    threshold_pct: float


async def evaluate_mode_drawdown(
    db: AsyncSession, sample: EquitySample, *, now: datetime | None = None
) -> MonitorOutcome:
    mode = sample.mode
    taken_at = now or datetime.now(UTC)

    await get_kill_switch_state(db, mode)  # creates the row on first use
    state = (
        await db.execute(
            select(KillSwitchState).where(KillSwitchState.mode == mode).with_for_update()
        )
    ).scalar_one()

    threshold = await get_effective_risk_limit(db, MAX_DRAWDOWN_LIMIT, DEFAULT_MAX_DRAWDOWN_PCT)
    state.threshold_pct = threshold

    if sample.equity is None:
        prior_peak = await peak_equity_since(db, mode, state.reset_at)
        record_snapshot(db, sample, peak_equity=prior_peak, drawdown_pct=None, taken_at=taken_at)
        state.updated_at = taken_at
        await db.commit()
        return MonitorOutcome("cannot_evaluate", sample, prior_peak, None, threshold)

    prior_peak = await peak_equity_since(db, mode, state.reset_at)
    peak = sample.equity if prior_peak is None else max(prior_peak, sample.equity)
    evaluation = evaluate_drawdown(
        current_equity=sample.equity, peak_equity=peak, threshold_pct=threshold
    )
    snapshot = record_snapshot(
        db, sample, peak_equity=peak, drawdown_pct=evaluation.drawdown_pct, taken_at=taken_at
    )
    state.last_drawdown_pct = evaluation.drawdown_pct
    state.updated_at = taken_at

    if state.tripped:
        await db.commit()
        return MonitorOutcome("already_tripped", sample, peak, evaluation.drawdown_pct, threshold)

    if not evaluation.should_trip:
        await db.commit()
        return MonitorOutcome("ok", sample, peak, evaluation.drawdown_pct, threshold)

    reason = (
        f"drawdown {evaluation.drawdown_pct:.2f}% from a peak equity of {peak:,.2f} "
        f"(now {sample.equity:,.2f}) reached the {threshold:.2f}% limit"
    )
    state.tripped = True
    state.tripped_at = taken_at
    state.trip_reason = reason
    await db.flush()  # the snapshot gets its id before the audit row refers to it
    await write_audit_entry(
        db,
        actor=MONITOR_ACTOR,
        action="kill_switch.tripped",
        entity_type="kill-switch",
        entity_id=mode,
        details={
            "reason": reason,
            "equity": sample.equity,
            "peak_equity": peak,
            "drawdown_pct": evaluation.drawdown_pct,
            "threshold_pct": threshold,
            "price_source": sample.price_source,
            "snapshot_id": str(snapshot.id),
        },
    )
    await db.commit()

    # Best-effort and after the commit: a failed notification must never undo the trip.
    await notify(
        AlertLevel.KILL_SWITCH,
        title=f"Kill switch tripped ({mode})",
        body=reason,
        details={"mode": mode, "threshold_pct": threshold, "drawdown_pct": evaluation.drawdown_pct},
    )
    return MonitorOutcome("tripped", sample, peak, evaluation.drawdown_pct, threshold)
