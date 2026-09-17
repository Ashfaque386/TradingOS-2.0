"""Persistence layer for the Max Drawdown Kill Switch (Build Spec §8).

Every function here takes an explicit `mode` ("live" | "paper") and reads/
writes exactly one row of `kill_switch_states` (unique on `mode`) -- the
live and paper switches never touch each other's row, so tripping one can
never trip or clear the other. `check_drawdown` is the only path that can
set `tripped=True`, and it never clears it; `reset_kill_switch` is the
*only* path that can set it back to `False`, and it always requires an
explicit human actor (`reset_by`) -- there is no code path in this module,
or anywhere that calls it, that clears a tripped switch without one.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.engine.risk.kill_switch import (
    DEFAULT_MAX_DRAWDOWN_PCT,
    KillSwitchMode,
    KillSwitchTrippedError,
    evaluate_drawdown,
)
from src.models.kill_switch_state import KillSwitchState
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel

__all__ = [
    "KillSwitchTrippedError",
    "assert_not_tripped",
    "check_drawdown",
    "get_kill_switch_state",
    "reset_kill_switch",
]


async def _get_or_create(db: AsyncSession, mode: KillSwitchMode) -> KillSwitchState:
    result = await db.execute(select(KillSwitchState).where(KillSwitchState.mode == mode))
    state = result.scalar_one_or_none()
    if state is None:
        state = KillSwitchState(mode=mode, tripped=False, threshold_pct=DEFAULT_MAX_DRAWDOWN_PCT)
        db.add(state)
        await db.commit()
        await db.refresh(state)
    return state


async def get_kill_switch_state(db: AsyncSession, mode: KillSwitchMode) -> KillSwitchState:
    return await _get_or_create(db, mode)


async def check_drawdown(
    db: AsyncSession,
    mode: KillSwitchMode,
    *,
    current_equity: float,
    peak_equity: float,
    threshold_pct: float | None = None,
) -> KillSwitchState:
    """Evaluate the current drawdown against the switch's threshold and
    persist the result. A switch that's already tripped is left alone --
    this function only ever moves `tripped` False -> True, never the
    reverse (see module docstring)."""
    state = await _get_or_create(db, mode)
    effective_threshold = threshold_pct if threshold_pct is not None else state.threshold_pct

    evaluation = evaluate_drawdown(
        current_equity=current_equity, peak_equity=peak_equity, threshold_pct=effective_threshold
    )

    state.last_drawdown_pct = evaluation.drawdown_pct
    state.threshold_pct = effective_threshold
    state.updated_at = datetime.now(UTC)

    newly_tripped = not state.tripped and evaluation.should_trip
    if newly_tripped:
        state.tripped = True
        state.tripped_at = datetime.now(UTC)
        state.trip_reason = (
            f"drawdown {evaluation.drawdown_pct:.4f}% >= threshold {effective_threshold:.4f}%"
        )

    await db.commit()
    await db.refresh(state)

    if newly_tripped:
        # Best-effort, outside the transaction that just committed the
        # trip itself -- a notification-send failure must never make the
        # trip appear to not have happened.
        await notify(
            AlertLevel.KILL_SWITCH,
            title=f"Kill switch tripped ({mode})",
            body=state.trip_reason or "drawdown threshold breached",
            details={"mode": mode, "threshold_pct": effective_threshold},
        )

    return state


async def reset_kill_switch(
    db: AsyncSession, mode: KillSwitchMode, *, reset_by: str
) -> KillSwitchState:
    """The only function in this codebase that may set `tripped=False`.
    `reset_by` is required and never defaulted -- an automated or anonymous
    reset is not a valid call, only a human-attributed one."""
    if not reset_by:
        raise ValueError("reset_kill_switch requires an explicit reset_by actor")

    state = await _get_or_create(db, mode)
    state.tripped = False
    state.trip_reason = None
    state.tripped_at = None
    state.reset_by = reset_by
    state.reset_at = datetime.now(UTC)
    state.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(state)
    return state


async def assert_not_tripped(db: AsyncSession, mode: KillSwitchMode) -> None:
    """The single choke point every order-intent-creating code path must
    call before creating an intent (src.orchestration.risk_gate does this
    first, ahead of every other check) -- proves the kill switch blocks
    intent *creation*, not just a later submission step."""
    state = await _get_or_create(db, mode)
    if state.tripped:
        raise KillSwitchTrippedError(mode, state.trip_reason)
