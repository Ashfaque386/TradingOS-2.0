"""Paper Trading Engine API (Build Spec §11): enroll a strategy version,
inspect its live position/fills, and two manual-trigger endpoints that
exercise the exact same Layer 1/Layer 2 functions the APScheduler jobs
call automatically (src.orchestration.paper_trading_scheduler) -- useful
for ops visibility and demonstration, never a required step: the engine
runs itself without either endpoint ever being called.
"""

import uuid

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    DailySignalResponse,
    EnrollPaperTradingRequest,
    PaperFillResponse,
    PaperPositionResponse,
    PaperTradingSubscriptionResponse,
    ProcessTickRequest,
    RunDailySignalRequest,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.daily_signal import DailySignal
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.user import User
from src.orchestration.paper_trading import (
    enroll_in_paper_trading,
    process_tick,
    run_daily_signal_generation,
)

router = APIRouter(prefix="/paper-trading", tags=["paper-trading"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("POST", "/api/v1/paper-trading/subscriptions", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/paper-trading/subscriptions/{subscription_id}", roles=list(Role))
register_policy(
    "GET", "/api/v1/paper-trading/subscriptions/{subscription_id}/position", roles=list(Role)
)
register_policy(
    "GET", "/api/v1/paper-trading/subscriptions/{subscription_id}/fills", roles=list(Role)
)
register_policy(
    "GET", "/api/v1/paper-trading/subscriptions/{subscription_id}/signals", roles=list(Role)
)
register_policy("POST", "/api/v1/paper-trading/daily-signal-run", roles=_OPERATOR_ROLES)
register_policy(
    "POST", "/api/v1/paper-trading/subscriptions/{subscription_id}/tick", roles=_OPERATOR_ROLES
)

# Module-level honest-stub providers, same instances the scheduler wires up
# in src.main's lifespan -- these manual-trigger endpoints exercise the
# real engine against the real (if fake-data-backed) pipeline, not a
# separate mocked-out demo path.
_PRICE_PROVIDER = FakeDailyPriceProvider()
_ORDER_BOOK_PROVIDER = MockOrderBookProvider()
_REGULATORY_PROVIDER = ReferenceTableRegulatoryDataProvider()


def _subscription_response(sub: PaperTradingSubscription) -> PaperTradingSubscriptionResponse:
    return PaperTradingSubscriptionResponse(
        id=sub.id,
        strategy_version_id=sub.strategy_version_id,
        symbol=sub.symbol,
        builtin_strategy=sub.builtin_strategy,
        sma_window=sub.sma_window,
        initial_capital=sub.initial_capital,
        stop_loss_pct=sub.stop_loss_pct,
        position_size_pct=sub.position_size_pct,
        is_active=sub.is_active,
    )


def _fill_response(fill: PaperFill) -> PaperFillResponse:
    return PaperFillResponse(
        id=fill.id,
        subscription_id=fill.subscription_id,
        symbol=fill.symbol,
        side=fill.side,
        order_group_id=fill.order_group_id,
        leg_index=fill.leg_index,
        requested_quantity=fill.requested_quantity,
        filled_quantity=fill.filled_quantity,
        avg_fill_price=fill.avg_fill_price,
        fully_filled=fill.fully_filled,
        realized_pnl=fill.realized_pnl,
        created_at=fill.created_at.isoformat(),
    )


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def enroll_subscription_endpoint(
    body: EnrollPaperTradingRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> PaperTradingSubscriptionResponse:
    subscription = await enroll_in_paper_trading(
        db,
        strategy_version_id=body.strategy_version_id,
        symbol=body.symbol,
        builtin_strategy=body.builtin_strategy,
        sma_window=body.sma_window,
        initial_capital=body.initial_capital,
        stop_loss_pct=body.stop_loss_pct,
        position_size_pct=body.position_size_pct,
        created_by=str(current_user.id),
    )
    return _subscription_response(subscription)


@router.get("/subscriptions/{subscription_id}")
async def get_subscription_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> PaperTradingSubscriptionResponse:
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    return _subscription_response(subscription)


@router.get("/subscriptions/{subscription_id}/position")
async def get_position_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> PaperPositionResponse:
    result = await db.execute(
        select(PaperPosition).where(PaperPosition.subscription_id == subscription_id)
    )
    position = result.scalar_one_or_none()
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No position yet")
    return PaperPositionResponse(
        subscription_id=position.subscription_id,
        symbol=position.symbol,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        realized_pnl=position.realized_pnl,
    )


@router.get("/subscriptions/{subscription_id}/fills")
async def list_fills_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[PaperFillResponse]:
    result = await db.execute(
        select(PaperFill)
        .where(PaperFill.subscription_id == subscription_id)
        .order_by(PaperFill.created_at)
    )
    return [_fill_response(fill) for fill in result.scalars().all()]


@router.get("/subscriptions/{subscription_id}/signals")
async def list_signals_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[DailySignalResponse]:
    result = await db.execute(
        select(DailySignal)
        .where(DailySignal.subscription_id == subscription_id)
        .order_by(DailySignal.generated_at)
    )
    return [
        DailySignalResponse(
            id=s.id,
            subscription_id=s.subscription_id,
            symbol=s.symbol,
            signal_type=s.signal_type,
            reference_price=s.reference_price,
            consumed=s.consumed,
        )
        for s in result.scalars().all()
    ]


@router.post("/daily-signal-run", status_code=status.HTTP_201_CREATED)
async def run_daily_signal_endpoint(
    body: RunDailySignalRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[DailySignalResponse]:
    as_of = pd.Timestamp(body.as_of) if body.as_of else pd.Timestamp.now().normalize()
    signals = await run_daily_signal_generation(db, price_provider=_PRICE_PROVIDER, as_of=as_of)
    return [
        DailySignalResponse(
            id=s.id,
            subscription_id=s.subscription_id,
            symbol=s.symbol,
            signal_type=s.signal_type,
            reference_price=s.reference_price,
            consumed=s.consumed,
        )
        for s in signals
    ]


@router.post("/subscriptions/{subscription_id}/tick")
async def process_tick_endpoint(
    subscription_id: uuid.UUID,
    body: ProcessTickRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> PaperFillResponse | None:
    fill = await process_tick(
        db,
        subscription_id=subscription_id,
        tick_price=body.tick_price,
        order_book_provider=_ORDER_BOOK_PROVIDER,
        regulatory_provider=_REGULATORY_PROVIDER,
    )
    return _fill_response(fill) if fill is not None else None
