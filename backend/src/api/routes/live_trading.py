"""Live Trading API (Build Spec §12): enroll a live-eligible strategy,
watch its sign-off queue in real time (`GET .../intents`), and
approve/reject individual intents -- the only two ways a `LiveOrderIntent`
ever reaches a broker or is safely disposed of, per-intent, every time.
The manual intent-generation trigger exercises the exact same
`generate_live_order_intent` function the scheduler
(`src.orchestration.live_trading_scheduler`) calls automatically; useful
for ops visibility and demonstration, never a required step -- the
pipeline generates intents on its own once a strategy is live-eligible
and enrolled.
"""

import uuid

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    CreateBatchAuthorizationRequest,
    EnrollLiveTradingRequest,
    GenerateLiveOrderIntentRequest,
    LiveBatchAuthorizationResponse,
    LiveOrderIntentResponse,
    LivePositionResponse,
    LiveTradingSubscriptionResponse,
    OrderResponse,
    RunDailySignalRequest,
    TradeResponse,
)
from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.order import Order
from src.models.trade import Trade
from src.models.user import User
from src.orchestration.live_trading import (
    IntentExpiredError,
    IntentNotFoundError,
    IntentNotPendingError,
    NoBrokerConfiguredError,
    StrategyNotLiveEligibleError,
    approve_live_order_intent,
    create_batch_authorization,
    enroll_in_live_trading,
    generate_live_order_intent,
    reject_live_order_intent,
    run_live_daily_signal_generation,
)

router = APIRouter(prefix="/live-trading", tags=["live-trading"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]
# Approving/rejecting a live order intent and authorizing a pre-approval
# batch are real-money actions -- same stricter pair as kill-switch reset
# and live-eligibility sign-off (src/api/routes/kill_switch.py,
# src/api/routes/strategies.py), not the broader _OPERATOR_ROLES above.
_LIVE_SIGNOFF_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("POST", "/api/v1/live-trading/subscriptions", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/live-trading/daily-signal-run", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/live-trading/subscriptions/{subscription_id}", roles=list(Role))
register_policy("GET", "/api/v1/live-trading/strategies/{strategy_id}/position", roles=list(Role))
register_policy("GET", "/api/v1/live-trading/strategies/{strategy_id}/orders", roles=list(Role))
register_policy("GET", "/api/v1/live-trading/orders/{order_id}/trades", roles=list(Role))
register_policy("GET", "/api/v1/live-trading/intents", roles=list(Role))
register_policy("GET", "/api/v1/live-trading/intents/{intent_id}", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/live-trading/subscriptions/{subscription_id}/generate-intent",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "POST", "/api/v1/live-trading/intents/{intent_id}/approve", roles=_LIVE_SIGNOFF_ROLES
)
register_policy(
    "POST", "/api/v1/live-trading/intents/{intent_id}/reject", roles=_LIVE_SIGNOFF_ROLES
)
register_policy("POST", "/api/v1/live-trading/batch-authorizations", roles=_LIVE_SIGNOFF_ROLES)

# Module-level honest-stub providers, same instances used by
# src.orchestration.live_trading_scheduler in src.main's lifespan.
_PRICE_PROVIDER = FakeDailyPriceProvider()
_REGULATORY_PROVIDER = ReferenceTableRegulatoryDataProvider()


def get_live_broker_adapter() -> BrokerAdapter | None:
    """A thin `Depends`-wrapped seam around `build_configured_adapter`
    (Phase 8), always production-pointed (`sandbox=False`) -- live
    trading must never share a code path with Shadow Mode's dedicated
    sandbox-pointed adapter. Exists as its own dependency (rather than
    calling the factory function inline in each route) purely so tests
    can override it the same way `src.api.routes.broker_credentials.
    get_broker_credentials_store` is overridden -- injecting a
    `httpx.MockTransport`-backed adapter without needing real broker
    credentials or network egress."""
    return build_configured_adapter(sandbox=False)


def _subscription_response(sub: LiveTradingSubscription) -> LiveTradingSubscriptionResponse:
    return LiveTradingSubscriptionResponse(
        id=sub.id,
        strategy_id=sub.strategy_id,
        symbol=sub.symbol,
        broker_name=sub.broker_name,
        builtin_strategy=sub.builtin_strategy,
        sma_window=sub.sma_window,
        initial_capital=sub.initial_capital,
        stop_loss_pct=sub.stop_loss_pct,
        position_size_pct=sub.position_size_pct,
        intent_expiry_seconds=sub.intent_expiry_seconds,
        is_active=sub.is_active,
    )


def _intent_response(intent: LiveOrderIntent) -> LiveOrderIntentResponse:
    return LiveOrderIntentResponse(
        id=intent.id,
        strategy_id=intent.strategy_id,
        symbol=intent.symbol,
        side=intent.side,
        quantity=intent.quantity,
        intent_type=intent.intent_type,
        generated_at=intent.generated_at.isoformat(),
        expires_at=intent.expires_at.isoformat(),
        status=intent.status,
        approved_by=intent.approved_by,
        approved_at=intent.approved_at.isoformat() if intent.approved_at else None,
        resulting_order_id=intent.resulting_order_id,
        batch_authorization_id=intent.batch_authorization_id,
    )


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def enroll_subscription_endpoint(
    body: EnrollLiveTradingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> LiveTradingSubscriptionResponse:
    try:
        subscription = await enroll_in_live_trading(
            db,
            strategy_id=body.strategy_id,
            symbol=body.symbol,
            broker_name=body.broker_name,
            builtin_strategy=body.builtin_strategy,
            sma_window=body.sma_window,
            initial_capital=body.initial_capital,
            stop_loss_pct=body.stop_loss_pct,
            position_size_pct=body.position_size_pct,
            intent_expiry_seconds=body.intent_expiry_seconds,
            created_by=str(current_user.id),
        )
    except StrategyNotLiveEligibleError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _subscription_response(subscription)


@router.post("/daily-signal-run", status_code=status.HTTP_200_OK)
async def run_daily_signal_endpoint(
    body: RunDailySignalRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[LiveTradingSubscriptionResponse]:
    as_of = pd.Timestamp(body.as_of) if body.as_of else pd.Timestamp.now().normalize()
    updated = await run_live_daily_signal_generation(
        db, price_provider=_PRICE_PROVIDER, as_of=as_of
    )
    return [_subscription_response(s) for s in updated]


@router.get("/subscriptions/{subscription_id}")
async def get_subscription_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> LiveTradingSubscriptionResponse:
    subscription = await db.get(LiveTradingSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return _subscription_response(subscription)


@router.get("/strategies/{strategy_id}/position")
async def get_position_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> LivePositionResponse:
    result = await db.execute(select(LivePosition).where(LivePosition.strategy_id == strategy_id))
    position = result.scalar_one_or_none()
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No position yet")
    return LivePositionResponse(
        strategy_id=position.strategy_id,
        symbol=position.symbol,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        realized_pnl=position.realized_pnl,
    )


@router.get("/strategies/{strategy_id}/orders")
async def list_orders_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[OrderResponse]:
    result = await db.execute(
        select(Order).where(Order.strategy_id == strategy_id).order_by(Order.submitted_at)
    )
    orders = result.scalars().all()
    return [
        OrderResponse(
            id=o.id,
            live_order_intent_id=o.live_order_intent_id,
            strategy_id=o.strategy_id,
            symbol=o.symbol,
            side=o.side,
            quantity=o.quantity,
            broker_name=o.broker_name,
            broker_order_id=o.broker_order_id,
            status=o.status,
            failure_reason=o.failure_reason,
            submitted_at=o.submitted_at.isoformat(),
        )
        for o in orders
    ]


@router.get("/orders/{order_id}/trades")
async def list_trades_endpoint(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[TradeResponse]:
    result = await db.execute(
        select(Trade).where(Trade.order_id == order_id).order_by(Trade.executed_at)
    )
    trades = result.scalars().all()
    return [
        TradeResponse(
            id=t.id,
            order_id=t.order_id,
            symbol=t.symbol,
            side=t.side,
            quantity=t.quantity,
            price=t.price,
            status=t.status,
            executed_at=t.executed_at.isoformat(),
        )
        for t in trades
    ]


@router.get("/intents")
async def list_intents_endpoint(
    strategy_id: uuid.UUID | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[LiveOrderIntentResponse]:
    query = select(LiveOrderIntent).order_by(LiveOrderIntent.generated_at.desc())
    if strategy_id is not None:
        query = query.where(LiveOrderIntent.strategy_id == strategy_id)
    if status_filter is not None:
        query = query.where(LiveOrderIntent.status == status_filter)
    result = await db.execute(query)
    return [_intent_response(i) for i in result.scalars().all()]


@router.get("/intents/{intent_id}")
async def get_intent_endpoint(
    intent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> LiveOrderIntentResponse:
    intent = await db.get(LiveOrderIntent, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Intent not found")
    return _intent_response(intent)


@router.post("/subscriptions/{subscription_id}/generate-intent")
async def generate_intent_endpoint(
    subscription_id: uuid.UUID,
    body: GenerateLiveOrderIntentRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
    adapter: BrokerAdapter | None = Depends(get_live_broker_adapter),
) -> LiveOrderIntentResponse | None:
    try:
        intent = await generate_live_order_intent(
            db,
            subscription_id=subscription_id,
            tick_price=body.tick_price,
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    except KillSwitchTrippedError as exc:
        raise HTTPException(status_code=status.HTTP_423_LOCKED, detail=str(exc)) from exc
    return _intent_response(intent) if intent is not None else None


@router.post("/intents/{intent_id}/approve")
async def approve_intent_endpoint(
    intent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
    adapter: BrokerAdapter | None = Depends(get_live_broker_adapter),
) -> LiveOrderIntentResponse:
    try:
        intent = await approve_live_order_intent(
            db,
            intent_id,
            approved_by=str(current_user.id),
            adapter=adapter,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntentExpiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except NoBrokerConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return _intent_response(intent)


@router.post("/intents/{intent_id}/reject")
async def reject_intent_endpoint(
    intent_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> LiveOrderIntentResponse:
    try:
        intent = await reject_live_order_intent(db, intent_id, rejected_by=str(current_user.id))
    except IntentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except IntentNotPendingError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _intent_response(intent)


@router.post("/batch-authorizations", status_code=status.HTTP_201_CREATED)
async def create_batch_authorization_endpoint(
    body: CreateBatchAuthorizationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> LiveBatchAuthorizationResponse:
    try:
        authorization = await create_batch_authorization(
            db,
            strategy_id=body.strategy_id,
            authorized_by=str(current_user.id),
            max_intents=body.max_intents,
            max_notional_per_intent=body.max_notional_per_intent,
            window_start=body.window_start,
            window_end=body.window_end,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return LiveBatchAuthorizationResponse(
        id=authorization.id,
        strategy_id=authorization.strategy_id,
        authorized_by=authorization.authorized_by,
        max_intents=authorization.max_intents,
        max_notional_per_intent=authorization.max_notional_per_intent,
        intents_used=authorization.intents_used,
        window_start=authorization.window_start.isoformat(),
        window_end=authorization.window_end.isoformat(),
    )
