"""Kill Switch API (Build Spec §8): read status, feed it a drawdown
observation (the call a real equity-monitoring loop -- Phase 7 -- will make
periodically), and the one and only way to clear a tripped switch: an
explicit, RBAC-gated human reset.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import CheckDrawdownRequest, KillSwitchStateResponse, ResetKillSwitchRequest
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.kill_switch import KillSwitchMode
from src.models.user import User
from src.orchestration.kill_switch import check_drawdown, get_kill_switch_state, reset_kill_switch

router = APIRouter(prefix="/kill-switch", tags=["kill-switch"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("GET", "/api/v1/kill-switch/{mode}", roles=list(Role))
register_policy("POST", "/api/v1/kill-switch/{mode}/check", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/kill-switch/{mode}/reset", roles=_OPERATOR_ROLES)


def _to_response(state) -> KillSwitchStateResponse:
    return KillSwitchStateResponse(
        mode=state.mode,
        tripped=state.tripped,
        trip_reason=state.trip_reason,
        last_drawdown_pct=state.last_drawdown_pct,
        threshold_pct=state.threshold_pct,
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
    _body: ResetKillSwitchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> KillSwitchStateResponse:
    state = await reset_kill_switch(db, mode, reset_by=str(current_user.id))
    return _to_response(state)
