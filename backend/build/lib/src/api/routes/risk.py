"""Order-intent and Go-Live readiness API (Build Spec §8). The order-intent
endpoint is what Phase 7's real live/paper trading engine will eventually
call for every order it wants to place -- this phase proves the gate
itself (kill switch -> compliance -> correlation) works end-to-end over
HTTP, ahead of that engine existing. Go-Live readiness is a pure
evaluation with no side effects: any authenticated role may check it,
mirroring how other non-mutating evaluations in this codebase (e.g.
backtest comparison) are open to `list(Role)`.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    GoLiveReadinessRequest,
    GoLiveReadinessResponse,
    OrderIntentRequest,
    OrderIntentResponse,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.go_live_gate import GoLiveReadinessInput, evaluate_go_live_readiness
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.user import User
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

router = APIRouter(prefix="/risk", tags=["risk"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("POST", "/api/v1/risk/order-intents", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/risk/go-live-readiness", roles=list(Role))

_REGULATORY_PROVIDER = ReferenceTableRegulatoryDataProvider()


@router.post("/order-intents", status_code=status.HTTP_201_CREATED)
async def create_order_intent_endpoint(
    body: OrderIntentRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> OrderIntentResponse:
    try:
        intent = await create_order_intent(
            db,
            mode=body.mode,
            symbol=body.symbol,
            side=body.side,
            quantity=body.quantity,
            proposed_price=body.proposed_price,
            reference_price=body.reference_price,
            proposed_position_value=body.proposed_position_value,
            portfolio_value=body.portfolio_value,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    except KillSwitchTrippedError as exc:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(exc)) from exc
    except OrderIntentRejectedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(exc.reasons)
        ) from exc

    return OrderIntentResponse(
        mode=intent.mode,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        price=intent.price,
        created_at=intent.created_at.isoformat(),
    )


@router.post("/go-live-readiness")
async def go_live_readiness_endpoint(
    body: GoLiveReadinessRequest,
    _current_user: User = Depends(require_role),
) -> GoLiveReadinessResponse:
    result = evaluate_go_live_readiness(
        GoLiveReadinessInput(
            num_trades=body.num_trades,
            calendar_days_running=body.calendar_days_running,
            clean_shadow_mode_streak_days=body.clean_shadow_mode_streak_days,
            live_win_rate=body.live_win_rate,
            backtest_win_rate=body.backtest_win_rate,
        ),
        min_trades=body.min_trades,
        min_calendar_days=body.min_calendar_days,
        min_clean_shadow_days=body.min_clean_shadow_days,
        max_win_rate_divergence_pp=body.max_win_rate_divergence_pp,
    )
    return GoLiveReadinessResponse(
        eligible=result.eligible, checks=result.checks, reasons=result.reasons
    )
