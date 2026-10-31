"""Kill Switch API (Build Spec §8): read status, feed it a drawdown
observation (the call a real equity-monitoring loop -- Phase 7 -- will make
periodically), and the one and only way to clear a tripped switch: an
explicit, RBAC-gated human reset.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import CheckDrawdownRequest, KillSwitchStateResponse, ResetKillSwitchRequest
from src.audit.service import write_audit_entry
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.kill_switch import KillSwitchMode
from src.models.kill_switch_state import KillSwitchState
from src.models.user import User
from src.orchestration.kill_switch import (
    KillSwitchNotTrippedError,
    check_drawdown,
    get_kill_switch_state,
    reset_kill_switch,
)

router = APIRouter(prefix="/kill-switch", tags=["kill-switch"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("GET", "/api/v1/kill-switch/{mode}", roles=list(Role))
register_policy("POST", "/api/v1/kill-switch/{mode}/check", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/kill-switch/{mode}/reset", roles=_OPERATOR_ROLES)


def _to_response(state: KillSwitchState) -> KillSwitchStateResponse:
    return KillSwitchStateResponse(
        mode=state.mode,
        tripped=state.tripped,
        trip_reason=state.trip_reason,
        last_drawdown_pct=state.last_drawdown_pct,
        threshold_pct=state.threshold_pct,
        tripped_at=state.tripped_at.isoformat() if state.tripped_at else None,
        reset_at=state.reset_at.isoformat() if state.reset_at else None,
        reset_reason=state.reset_reason,
    )


@router.get("/{mode}")
async def get_kill_switch_endpoint(
    mode: KillSwitchMode,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> KillSwitchStateResponse:
    state = await get_kill_switch_state(db, mode)
    return _to_response(state)


@router.post("/{mode}/check")
async def check_drawdown_endpoint(
    mode: KillSwitchMode,
    body: CheckDrawdownRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> KillSwitchStateResponse:
    state = await check_drawdown(
        db,
        mode,
        current_equity=body.current_equity,
        peak_equity=body.peak_equity,
        threshold_pct=body.threshold_pct,
    )
    return _to_response(state)


@router.post("/{mode}/reset")
async def reset_kill_switch_endpoint(
    mode: KillSwitchMode,
    body: ResetKillSwitchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> KillSwitchStateResponse:
    reason = (body.reason or "").strip() or None
    tripped_state = await get_kill_switch_state(db, mode)
    why_it_tripped = tripped_state.trip_reason
    drawdown_at_trip = tripped_state.last_drawdown_pct
    try:
        state = await reset_kill_switch(db, mode, reset_by=str(current_user.id), reason=reason)
    except KillSwitchNotTrippedError as exc:
        # 409, not a 200 no-op: the audit middleware writes a row for every 2xx,
        # and a "reset" row for a switch that was not tripped would put a reset
        # in the permanent trail that never happened.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc).capitalize()) from exc
    # The audit middleware records only that a reset happened; the reason a person gave, and what
    # they were resetting, belong in the permanent trail too.
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="kill_switch.reset",
        entity_type="kill-switch",
        entity_id=mode,
        details={
            "reason": reason,
            "tripped_because": why_it_tripped,
            "drawdown_pct_at_trip": drawdown_at_trip,
        },
    )
    await db.commit()
    return _to_response(state)
