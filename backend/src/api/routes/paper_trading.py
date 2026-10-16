"""Paper Trading Engine API (Build Spec §11): enroll a strategy version,
inspect its live position/fills, and two manual-trigger endpoints that
exercise the exact same Layer 1/Layer 2 functions the APScheduler jobs
call automatically (src.orchestration.paper_trading_scheduler) -- useful
for ops visibility and demonstration, never a required step: the engine
runs itself without either endpoint ever being called.
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    ClosePaperPositionRequest,
    DailySignalResponse,
    DeactivatePaperSubscriptionRequest,
    DeactivatePaperSubscriptionResponse,
    EnrollPaperTradingRequest,
    PaperFillResponse,
    PaperHistoryBackfillResponse,
    PaperLastTick,
    PaperPositionClosedResponse,
    PaperPositionResponse,
    PaperSubscriptionDetailResponse,
    PaperTradingSubscriptionResponse,
    ProcessTickRequest,
    RunDailySignalRequest,
    TodaysPaperPnlResponse,
    UnrealizedPaperPnlResponse,
)
from src.brokers.tick_source import is_broker_configured
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.data.broker_provider import NoRealDataSourceError
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.data.providers import AnyMarketDataProvider
from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.tick_feed import get_latest_tick
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.daily_signal import DailySignal
from src.models.data_source import DataSource
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.user import User
from src.orchestration.market_data import run_symbol_history_backfill
from src.orchestration.paper_subscription_controls import (
    CloseBlockedError,
    CloseResult,
    PriceUnavailableError,
    SubscriptionNotFoundError,
    SubscriptionStateError,
    SubscriptionView,
    activate_subscription,
    assert_no_active_duplicate,
    close_position,
    deactivate_subscription,
    list_subscription_views,
)
from src.orchestration.paper_trading import (
    enroll_in_paper_trading,
    process_tick,
    run_daily_signal_generation,
)

router = APIRouter(prefix="/paper-trading", tags=["paper-trading"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("POST", "/api/v1/paper-trading/subscriptions", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/paper-trading/subscriptions", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/deactivate",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "POST",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/activate",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "POST",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/close-position",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "POST",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/backfill-history",
    roles=_OPERATOR_ROLES,
)
register_policy("GET", "/api/v1/paper-trading/pnl/today", roles=list(Role))
register_policy("GET", "/api/v1/paper-trading/pnl/unrealized", roles=list(Role))
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

# Module-level stand-ins for the order book and regulatory table, the same kind the scheduler
# wires up in src.main's lifespan. (The daily-signal price provider is a dependency --
# `get_signal_price_provider` -- so a manual run reads the real lake, never generated bars.)
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
        data_source=fill.data_source,
        created_at=fill.created_at.isoformat(),
    )


@router.get("/pnl/today")
async def todays_paper_pnl_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> TodaysPaperPnlResponse:
    """Realized P&L only -- a real, honest aggregate of `PaperFill.realized_pnl`
    (the per-fill delta `_persist_fill` already writes alongside the
    cumulative total it adds to `PaperPosition.realized_pnl`) for fills
    created since IST midnight today. Deliberately does not attempt an
    unrealized/mark-to-market figure: that would need a genuine current
    price per open position, which this engine has no non-synthetic source
    for outside of a configured broker's live quote -- see
    docs/phase16-wiring-audit.md Follow-up F for the reasoning."""
    start_of_day_ist = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(
            func.coalesce(func.sum(PaperFill.realized_pnl), 0.0),
            func.count(PaperFill.id),
        ).where(PaperFill.created_at >= start_of_day_ist)
    )
    total_realized, fill_count = result.one()
    return TodaysPaperPnlResponse(
        as_of_date=start_of_day_ist.date().isoformat(),
        realized_pnl=float(total_realized),
        fill_count=fill_count,
    )


def _price_source() -> Literal["real", "synthetic", "none"]:
    if is_broker_configured():
        return "real"
    return "synthetic" if get_settings().paper_trading_synthetic else "none"


@router.get("/pnl/unrealized")
async def unrealized_paper_pnl_endpoint(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> UnrealizedPaperPnlResponse:
    """Mark-to-market P&L (docs/phase16-wiring-audit.md Follow-up F, the
    part /pnl/today deliberately leaves out) for every currently-open
    position (`PaperPosition.quantity != 0`), priced off the last tick on
    each symbol's `paper:ticks:{symbol}` stream -- whichever `TickSource`
    is actually feeding paper trading right now, real broker quotes or
    `MockTickSource`'s synthetic walk. `price_source` says which one, so
    the two are never blended into one unlabeled figure. Not date-scoped,
    unlike /pnl/today: an open position may have been opened on an
    earlier day and is still marked as of now."""
    result = await db.execute(
        select(PaperPosition, PaperTradingSubscription.is_active)
        .join(
            PaperTradingSubscription, PaperPosition.subscription_id == PaperTradingSubscription.id
        )
        .where(PaperPosition.quantity != 0)
    )
    rows = result.all()
    open_positions = [position for position, _ in rows]
    unmanaged_positions = sum(1 for _, is_active in rows if not is_active)

    positions_priced = 0
    positions_unpriced = 0
    priced_total = 0.0
    for position in open_positions:
        tick = await get_latest_tick(redis, position.symbol)
        if tick is None:
            positions_unpriced += 1
            continue
        positions_priced += 1
        priced_total += (tick.price - position.avg_cost) * position.quantity

    if not open_positions:
        unrealized_pnl = 0.0
    elif positions_priced == 0:
        unrealized_pnl = None
    else:
        unrealized_pnl = priced_total

    return UnrealizedPaperPnlResponse(
        as_of=datetime.now(UTC).isoformat(),
        price_source=_price_source(),
        unrealized_pnl=unrealized_pnl,
        positions_priced=positions_priced,
        positions_unpriced=positions_unpriced,
        unmanaged_positions=unmanaged_positions,
    )


@router.post("/subscriptions", status_code=status.HTTP_201_CREATED)
async def enroll_subscription_endpoint(
    body: EnrollPaperTradingRequest,
    db: AsyncSession = Depends(get_db),
    provider: AnyMarketDataProvider = Depends(get_market_data_provider),
    current_user: User = Depends(require_role),
) -> PaperTradingSubscriptionResponse:
    listed = await _symbol_listing(provider, body.symbol)
    if listed is False:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"no connected broker lists an NSE equity or index named {body.symbol!r}: paper "
                "trading runs on real symbols"
            ),
        )
    try:
        await assert_no_active_duplicate(
            db, strategy_version_id=body.strategy_version_id, symbol=body.symbol
        )
    except SubscriptionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
    if listed is True:
        # Fetch its real price history now so it can signal tomorrow morning. Best effort: a
        # failure is recorded (and shown as `awaiting_history`), it does not undo the enrolment --
        # the 07:30 gap repair and `backfill-history` try again.
        await run_symbol_history_backfill(
            db,
            provider=provider,
            root=Path(get_settings().data_lake_path),
            symbol=subscription.symbol,
            as_of=datetime.now(IST).date(),
        )
    return _subscription_response(subscription)


async def _symbol_listing(provider: AnyMarketDataProvider, symbol: str) -> bool | None:
    """True/False: a connected broker does/does not list the symbol. None: cannot tell (no broker
    is connected, or the provider is the generated-data one) -- the subscription is then allowed
    and its state says why it is not running."""
    check = getattr(provider, "symbol_listed", None)
    if check is None:
        return None
    try:
        return bool(await check(symbol))
    except NoRealDataSourceError:
        return None


def _detail_response(view: SubscriptionView) -> PaperSubscriptionDetailResponse:
    sub = view.subscription
    return PaperSubscriptionDetailResponse(
        id=sub.id,
        strategy_version_id=sub.strategy_version_id,
        symbol=sub.symbol,
        builtin_strategy=sub.builtin_strategy,
        sma_window=sub.sma_window,
        initial_capital=sub.initial_capital,
        stop_loss_pct=sub.stop_loss_pct,
        position_size_pct=sub.position_size_pct,
        is_active=sub.is_active,
        state=view.state,
        state_reason=view.state_reason,
        created_at=sub.created_at.isoformat(),
        created_by=sub.created_by,
        deactivated_at=sub.deactivated_at.isoformat() if sub.deactivated_at else None,
        deactivated_by=sub.deactivated_by,
        deactivation_reason=sub.deactivation_reason,
        quantity=view.quantity,
        avg_cost=view.avg_cost,
        realized_pnl=view.realized_pnl,
        managed=sub.is_active or view.quantity == 0,
        last_tick=(
            PaperLastTick(
                price=view.last_tick.price,
                age_seconds=view.last_tick_age_seconds or 0.0,
                source=view.last_tick.source,
            )
            if view.last_tick is not None
            else None
        ),
        unrealized_pnl=view.unrealized_pnl,
    )


def _closed_response(result: CloseResult) -> PaperPositionClosedResponse:
    position = result.position
    return PaperPositionClosedResponse(
        fill=_fill_response(result.fill),
        position=PaperPositionResponse(
            subscription_id=position.subscription_id,
            symbol=position.symbol,
            quantity=position.quantity,
            avg_cost=position.avg_cost,
            realized_pnl=position.realized_pnl,
        ),
        closed=position.quantity == 0,
        remaining_quantity=result.remaining_quantity,
        price=result.price,
        price_basis=result.price_basis,
        data_source=result.data_source,
        administrative=result.administrative,
    )


def _control_error(exc: Exception) -> HTTPException:
    """The one mapping from a control's refusal to an HTTP status: an unknown id is 404; a request
    that changes nothing or conflicts with the state (a risk-gate refusal included) is 409; no
    usable price is 422."""
    if isinstance(exc, SubscriptionNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    if isinstance(exc, PriceUnavailableError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))


@router.post("/subscriptions/{subscription_id}/backfill-history")
async def backfill_history_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    provider: AnyMarketDataProvider = Depends(get_market_data_provider),
    _current_user: User = Depends(require_role),
) -> PaperHistoryBackfillResponse:
    """(Re)fetch this subscription's real price history from the connected broker. It replaces any
    generated or untagged rows the symbol has, and keeps the symbol on the broker its history
    started with. Safe to repeat. The response says what happened, including a failure."""
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    row = await run_symbol_history_backfill(
        db,
        provider=provider,
        root=Path(get_settings().data_lake_path),
        symbol=subscription.symbol,
        as_of=datetime.now(IST).date(),
    )
    details = row.details or {}
    return PaperHistoryBackfillResponse(
        status="success" if row.status == "success" else "failed",
        symbol=subscription.symbol,
        rows=row.rows_ingested,
        first_date=details.get("first_date"),
        last_date=details.get("last_date"),
        source=details.get("source"),
        unreal_rows_removed=int(details.get("unreal_rows_removed", 0)),
        error=row.error_message,
    )


@router.get("/subscriptions")
async def list_subscriptions_endpoint(
    active: bool | None = None,
    symbol: str | None = None,
    state: Literal["active", "awaiting_history", "halted_no_data", "inactive"] | None = None,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    price_provider: DataLakePriceProvider = Depends(get_signal_price_provider),
    _current_user: User = Depends(require_role),
) -> list[PaperSubscriptionDetailResponse]:
    views = await list_subscription_views(
        db, redis, price_provider, active=active, symbol=symbol, state=state
    )
    return [_detail_response(view) for view in views]


@router.post("/subscriptions/{subscription_id}/deactivate")
async def deactivate_subscription_endpoint(
    subscription_id: uuid.UUID,
    body: DeactivatePaperSubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(require_role),
) -> DeactivatePaperSubscriptionResponse:
    try:
        result = await deactivate_subscription(
            db,
            redis,
            subscription_id,
            actor=str(current_user.id),
            reason=body.reason,
            on_open_position=body.on_open_position,
            close_price=body.close_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    except (SubscriptionNotFoundError, SubscriptionStateError, PriceUnavailableError) as exc:
        raise _control_error(exc) from exc
    return DeactivatePaperSubscriptionResponse(
        subscription=_subscription_response(result.subscription),
        closed=_closed_response(result.closed) if result.closed else None,
        left_unmanaged_quantity=result.left_unmanaged_quantity,
    )


@router.post("/subscriptions/{subscription_id}/activate")
async def activate_subscription_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(require_role),
) -> PaperTradingSubscriptionResponse:
    try:
        subscription = await activate_subscription(
            db, redis, subscription_id, actor=str(current_user.id)
        )
    except (SubscriptionNotFoundError, SubscriptionStateError) as exc:
        raise _control_error(exc) from exc
    return _subscription_response(subscription)


@router.post("/subscriptions/{subscription_id}/close-position")
async def close_position_endpoint(
    subscription_id: uuid.UUID,
    body: ClosePaperPositionRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(require_role),
) -> PaperPositionClosedResponse:
    if body.administrative and current_user.role != Role.SYSTEM_ADMINISTRATOR:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="an administrative close (no risk gate) needs the SystemAdministrator role",
        )
    try:
        result = await close_position(
            db,
            redis,
            subscription_id,
            actor=str(current_user.id),
            reason=body.reason,
            price=body.price,
            administrative=body.administrative,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    except (
        SubscriptionNotFoundError,
        SubscriptionStateError,
        CloseBlockedError,
        PriceUnavailableError,
    ) as exc:
        raise _control_error(exc) from exc
    return _closed_response(result)


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
    price_provider: DataLakePriceProvider = Depends(get_signal_price_provider),
) -> list[DailySignalResponse]:
    as_of = pd.Timestamp(body.as_of) if body.as_of else pd.Timestamp.now().normalize()
    signals = await run_daily_signal_generation(db, price_provider=price_provider, as_of=as_of)
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
        tick_source=DataSource.SUPPLIED,  # an operator typed the price
        order_book_provider=_ORDER_BOOK_PROVIDER,
        regulatory_provider=_REGULATORY_PROVIDER,
    )
    return _fill_response(fill) if fill is not None else None
