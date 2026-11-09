"""Paper Trading Engine API (Build Spec §11): enroll a strategy version,
inspect its live position/fills, and two manual-trigger endpoints that
exercise the exact same Layer 1/Layer 2 functions the APScheduler jobs
call automatically (src.orchestration.paper_trading_scheduler) -- useful
for ops visibility and demonstration, never a required step: the engine
runs itself without either endpoint ever being called.
"""

import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, cast

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    ClosePaperPositionRequest,
    DailySignalResponse,
    DeactivatePaperSubscriptionRequest,
    DeactivatePaperSubscriptionResponse,
    EnrollPaperTradingRequest,
    EnrolPaperPreviewRequest,
    PaperClosePreviewResponse,
    PaperEnrolPreviewResponse,
    PaperFillResponse,
    PaperHistoryBackfillResponse,
    PaperLastTick,
    PaperPositionClosedResponse,
    PaperPositionResponse,
    PaperQuoteRefusal,
    PaperSettingChange,
    PaperSubscriptionDetailResponse,
    PaperSymbolHitResponse,
    PaperSymbolSearchResponse,
    PaperTradingSubscriptionResponse,
    ProcessTickRequest,
    RunDailySignalRequest,
    TodaysPaperPnlResponse,
    UnrealizedPaperPnlResponse,
    UpdatePaperSettingsRequest,
    UpdatePaperSettingsResponse,
)
from src.brokers.base import FuturesContract
from src.brokers.futures_contracts import NoFuturesContractError
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.core.redis_client import get_redis
from src.data.broker_provider import NoRealDataSourceError
from src.data.market_data_factory import get_market_data_provider
from src.data.price_provider import DataLakePriceProvider, get_signal_price_provider
from src.data.providers import AnyMarketDataProvider
from src.engine.paper_trading.depth import build_order_book_provider
from src.engine.paper_trading.margin import check_futures_margin
from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.option_strategies import pick_expiry
from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.tick_feed import Tick, get_latest_tick
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.daily_signal import DailySignal
from src.models.data_source import DataSource
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.user import User
from src.orchestration.equity import paper_price_source
from src.orchestration.market_data import run_symbol_history_backfill
from src.orchestration.paper_desk_previews import (
    preview_close,
    preview_enrolment,
    symbol_listing,
)
from src.orchestration.paper_subscription_controls import (
    CloseBlockedError,
    CloseResult,
    PriceUnavailableError,
    SubscriptionNotFoundError,
    SubscriptionStateError,
    SubscriptionView,
    activate_subscription,
    assert_no_active_duplicate,
    close_all_positions,
    close_position,
    deactivate_subscription,
    list_subscription_views,
    update_settings,
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
register_policy("GET", "/api/v1/paper-trading/symbols", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/paper-trading/enrol-preview", roles=_OPERATOR_ROLES)
register_policy(
    "GET",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/close-preview",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "PATCH",
    "/api/v1/paper-trading/subscriptions/{subscription_id}/settings",
    roles=_OPERATOR_ROLES,
)
register_policy("GET", "/api/v1/paper-trading/pnl/today", roles=list(Role))
register_policy("GET", "/api/v1/paper-trading/pnl/unrealized", roles=list(Role))
register_policy("GET", "/api/v1/paper-trading/subscriptions/{subscription_id}", roles=list(Role))
register_policy(
    "GET", "/api/v1/paper-trading/subscriptions/{subscription_id}/position", roles=list(Role)
)
register_policy(
    "GET", "/api/v1/paper-trading/subscriptions/{subscription_id}/positions", roles=list(Role)
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

# The operator-supplied tick endpoint fills against a generated book (it is a test tool: the price
# is typed, and the fill is labelled so); deactivate/close use the real depth. The regulatory
# table is the same kind the scheduler wires up in src.main's lifespan. (The daily-signal price
# provider is a dependency -- `get_signal_price_provider` -- so a manual run reads the real lake,
# never generated bars.)
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
        instrument_kind=sub.instrument_kind,  # type: ignore[arg-type]
        contract_symbol=sub.contract_symbol,
        contract_expiry=sub.contract_expiry.isoformat() if sub.contract_expiry else None,
        lot_size=sub.lot_size,
        lots=sub.lots,
        option_strategy=sub.option_strategy,
        spread_width=sub.spread_width,
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
        costs_total=fill.costs_total,
        costs_breakdown=fill.costs_breakdown,
        data_source=fill.data_source,
        administrative=fill.administrative,
        created_at=fill.created_at.isoformat(),
    )


def _signal_response(signal: DailySignal) -> DailySignalResponse:
    return DailySignalResponse(
        id=signal.id,
        subscription_id=signal.subscription_id,
        symbol=signal.symbol,
        signal_type=signal.signal_type,
        reference_price=signal.reference_price,
        consumed=signal.consumed,
        generated_at=signal.generated_at.isoformat(),
        consumed_at=signal.consumed_at.isoformat() if signal.consumed_at else None,
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
            func.coalesce(func.sum(PaperFill.costs_total), 0.0),
            func.count(PaperFill.id),
        ).where(PaperFill.created_at >= start_of_day_ist)
    )
    total_realized, total_costs, fill_count = result.one()
    return TodaysPaperPnlResponse(
        as_of_date=start_of_day_ist.date().isoformat(),
        realized_pnl=float(total_realized),
        costs_total=float(total_costs),
        net_realized_pnl=float(total_realized) - float(total_costs),
        fill_count=fill_count,
    )


_price_source = paper_price_source


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
    listed = await symbol_listing(provider, body.symbol)
    if listed is False:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"no connected broker lists an NSE equity or index named {body.symbol!r}: paper "
                "trading runs on real symbols"
            ),
        )
    contract = None
    if body.instrument_kind == "future":
        contract = await _future_contract_for(provider, body)
    if body.instrument_kind == "option":
        await _check_options_listed(provider, body.symbol)
    try:
        await assert_no_active_duplicate(
            db,
            strategy_version_id=body.strategy_version_id,
            symbol=body.symbol,
            instrument_kind=body.instrument_kind,
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
        stop_loss_pct=(
            body.stop_loss_pct
            if body.stop_loss_pct is not None
            else (OPTION_DEFAULT_STOP_LOSS_PCT if body.instrument_kind == "option" else 3.0)
        ),
        position_size_pct=body.position_size_pct,
        created_by=str(current_user.id),
        contract=contract,
        lots=body.lots,
        option_strategy=body.option_strategy if body.instrument_kind == "option" else None,
        spread_width=body.spread_width,
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


OPTION_DEFAULT_STOP_LOSS_PCT = 50.0


async def _check_options_listed(provider: AnyMarketDataProvider, underlying: str) -> None:
    """An option subscription needs the connected broker's contract list (the only source of
    expiries, strikes and lot sizes), and an expiry far enough out to enter. Refused otherwise,
    not guessed."""
    lister = getattr(provider, "option_contracts", None)
    if lister is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "options need a connected broker: its instrument master is the only source of "
                "the contracts and their lot sizes"
            ),
        )
    try:
        contracts = await lister(underlying)
    except NoRealDataSourceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"options need a connected broker ({exc})",
        ) from exc
    settings = get_settings()
    if (
        pick_expiry(
            contracts,
            today=datetime.now(IST).date(),
            min_days=settings.paper_options_min_days_to_expiry,
        )
        is None
    ):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"no listed {underlying} option expires more than "
                f"{settings.paper_options_min_days_to_expiry} days from now"
                f"{' (the broker lists none)' if not contracts else ''}"
            ),
        )


async def _future_contract_for(
    provider: AnyMarketDataProvider, body: EnrollPaperTradingRequest
) -> FuturesContract:
    """The contract a new futures subscription will trade, and a check that one entry fits the
    account. Both need the connected broker (its instrument master is the only source of the
    contract and the lot size), so a futures enrolment without one is refused rather than
    guessed. The margin check uses an indicative price and is skipped if nobody quotes one."""
    resolver = getattr(provider, "futures_contract", None)
    if resolver is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "futures need a connected broker: its instrument master is the only source of "
                "the contract and its lot size"
            ),
        )
    settings = get_settings()
    try:
        contract = cast(
            FuturesContract,
            await resolver(
                body.symbol,
                today=datetime.now(IST).date(),
                roll_days=settings.paper_futures_roll_days,
            ),
        )
    except NoRealDataSourceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"futures need a connected broker ({exc})",
        ) from exc
    except NoFuturesContractError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    price = await provider.indicative_price(contract.symbol)  # type: ignore[union-attr]
    if price:
        margin = check_futures_margin(
            price=price,
            quantity=body.lots * contract.lot_size,
            capital=body.initial_capital,
            margin_pct=settings.paper_futures_margin_pct,
            max_utilisation_pct=settings.paper_futures_max_margin_utilisation_pct,
        )
        if not margin.ok:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"{body.lots} lot(s) of {contract.symbol} ({contract.lot_size} each) do not "
                    f"fit this account: {margin.reason}. Raise initial_capital or trade fewer lots."
                ),
            )
    return contract


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
        instrument_kind=sub.instrument_kind,  # type: ignore[arg-type]
        contract_symbol=sub.contract_symbol,
        contract_expiry=sub.contract_expiry.isoformat() if sub.contract_expiry else None,
        lot_size=sub.lot_size,
        lots=sub.lots,
        option_strategy=sub.option_strategy,
        spread_width=sub.spread_width,
        open_legs=view.open_legs,
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
        costs_total=view.costs_total,
        net_realized_pnl=view.realized_pnl - view.costs_total,
        managed=view.managed,
        last_tick=(
            _last_tick_response(view.last_tick, view.last_tick_age_seconds)
            if view.last_tick is not None
            else None
        ),
        unrealized_pnl=view.unrealized_pnl,
        quote_refusal=(
            PaperQuoteRefusal(
                broker=view.quote_refusal.broker,
                reason=view.quote_refusal.reason,
                exchange_time=view.quote_refusal.exchange_time,
                at=view.quote_refusal.at,
            )
            if view.quote_refusal is not None
            else None
        ),
        circuit_fallback_band_pct=_REGULATORY_PROVIDER.circuit_filter_band_pct(sub.symbol),
    )


def _last_tick_response(tick: Tick, age_seconds: float | None) -> PaperLastTick:
    exchange_time = (
        datetime.fromtimestamp(tick.exchange_time_ms / 1000.0, tz=UTC)
        if tick.exchange_time_ms is not None
        else None
    )
    return PaperLastTick(
        price=tick.price,
        age_seconds=age_seconds or 0.0,
        source=tick.source,
        exchange_time=exchange_time,
        exchange_age_seconds=(
            max(0.0, (datetime.now(UTC) - exchange_time).total_seconds()) if exchange_time else None
        ),
        lower_circuit=tick.lower_circuit,
        upper_circuit=tick.upper_circuit,
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
            costs_total=position.costs_total,
            net_realized_pnl=position.realized_pnl - position.costs_total,
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
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
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
            order_book_provider=build_order_book_provider(redis),
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
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    try:
        if subscription is not None and subscription.is_option:
            return await _close_every_leg(db, redis, subscription_id, body, current_user)
        result = await close_position(
            db,
            redis,
            subscription_id,
            actor=str(current_user.id),
            reason=body.reason,
            price=body.price,
            administrative=body.administrative,
            order_book_provider=build_order_book_provider(redis),
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


async def _close_every_leg(
    db: AsyncSession,
    redis: Redis,
    subscription_id: uuid.UUID,
    body: ClosePaperPositionRequest,
    current_user: User,
) -> PaperPositionClosedResponse:
    """The close of an option strategy: every open leg, sold legs first. A leg that cannot close
    (no tick, no book, a refusal) is listed in `failures`; if none could, it is a 409."""
    outcome = await close_all_positions(
        db,
        redis,
        subscription_id,
        actor=str(current_user.id),
        reason=body.reason,
        price=body.price,
        administrative=body.administrative,
        order_book_provider=build_order_book_provider(redis),
        regulatory_provider=_REGULATORY_PROVIDER,
    )
    if not outcome.closed:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="; ".join(outcome.failures))
    response = _closed_response(outcome.closed[0])
    response.closed = outcome.all_flat
    response.remaining_quantity = sum(r.remaining_quantity for r in outcome.closed)
    response.legs = [_fill_response(r.fill) for r in outcome.closed]
    response.failures = outcome.failures
    return response


@router.get("/symbols")
async def search_symbols_endpoint(
    q: str = Query(min_length=1, max_length=32),
    limit: int = Query(default=10, ge=1, le=25),
    provider: AnyMarketDataProvider = Depends(get_market_data_provider),
    _current_user: User = Depends(require_role),
) -> PaperSymbolSearchResponse:
    """Listed NSE equities and indices matching `q`, from the connected brokers' own instrument
    lists. With no broker connected there is nothing to search (`available: false`); the desk then
    takes a typed symbol."""
    searcher = getattr(provider, "search_symbols", None)
    if searcher is None:
        return PaperSymbolSearchResponse(
            available=False, reason="No broker is connected, so symbols can't be searched."
        )
    try:
        hits = await searcher(q, limit)
    except NoRealDataSourceError as exc:
        return PaperSymbolSearchResponse(available=False, reason=str(exc))
    return PaperSymbolSearchResponse(
        available=True,
        symbols=[PaperSymbolHitResponse(symbol=hit.symbol, kind=hit.kind) for hit in hits],
    )


@router.get("/enrol-preview")
async def enrol_preview_endpoint(
    body: Annotated[EnrolPaperPreviewRequest, Query()],
    db: AsyncSession = Depends(get_db),
    provider: AnyMarketDataProvider = Depends(get_market_data_provider),
    _current_user: User = Depends(require_role),
) -> PaperEnrolPreviewResponse:
    """What enrolling this would do -- whether the symbol is listed, the contract and lot size, the
    entry it would size, the margin or largest loss against the account -- without enrolling it.
    `problems` are what the real enrolment would refuse; nothing is created. A GET (the same
    fields as an enrolment, as query parameters): it changes nothing, so it must not leave a row in
    the audit trail for every keystroke-debounced preview."""
    preview = await preview_enrolment(
        db,
        provider,
        symbol=body.symbol,
        instrument_kind=body.instrument_kind,
        initial_capital=body.initial_capital,
        position_size_pct=body.position_size_pct,
        lots=body.lots,
        option_strategy=body.option_strategy,
        spread_width=body.spread_width,
        strategy_version_id=body.strategy_version_id,
    )
    return PaperEnrolPreviewResponse.model_validate(asdict(preview))


@router.get("/subscriptions/{subscription_id}/close-preview")
async def close_preview_endpoint(
    subscription_id: uuid.UUID,
    price: float | None = Query(default=None, gt=0),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> PaperClosePreviewResponse:
    """What closing the position would do right now: per leg, the price it would use, the book it
    would walk, the estimated fill, its costs and result, and why it would be refused. Read-only.
    `price` is a price the operator would supply (for a one-position subscription), as in the
    close itself."""
    try:
        preview = await preview_close(
            db,
            redis,
            subscription_id,
            order_book_provider=build_order_book_provider(redis),
            price=price,
        )
    except (SubscriptionNotFoundError, SubscriptionStateError) as exc:
        raise _control_error(exc) from exc
    return PaperClosePreviewResponse.model_validate(asdict(preview))


@router.patch("/subscriptions/{subscription_id}/settings")
async def update_settings_endpoint(
    subscription_id: uuid.UUID,
    body: UpdatePaperSettingsRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    current_user: User = Depends(require_role),
) -> UpdatePaperSettingsResponse:
    """Changes the settings that may change after a subscription is added (stop-loss, size, lots,
    hedge distance, signal, average length; capital only before its first fill). A reason is
    required and the before and after values are audited. What the subscription trades never
    changes: switch it off and add a new one for that."""
    requested = {
        name: value
        for name, value in body.model_dump(exclude={"reason"}).items()
        if value is not None
    }
    try:
        subscription, changes = await update_settings(
            db,
            redis,
            subscription_id,
            actor=str(current_user.id),
            reason=body.reason,
            requested=requested,
        )
    except (SubscriptionNotFoundError, SubscriptionStateError) as exc:
        raise _control_error(exc) from exc
    return UpdatePaperSettingsResponse(
        subscription=_subscription_response(subscription),
        changes=[
            PaperSettingChange(field=c.field, before=c.before, after=c.after) for c in changes
        ],
    )


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
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is not None and subscription.is_option:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="an option subscription holds a position per leg: use GET .../positions",
        )
    result = await db.execute(
        select(PaperPosition).where(PaperPosition.subscription_id == subscription_id)
    )
    position = result.scalar_one_or_none()
    if position is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No position yet")
    return _position_response(position)


def _position_response(position: PaperPosition) -> PaperPositionResponse:
    return PaperPositionResponse(
        subscription_id=position.subscription_id,
        symbol=position.symbol,
        quantity=position.quantity,
        avg_cost=position.avg_cost,
        realized_pnl=position.realized_pnl,
        costs_total=position.costs_total,
        net_realized_pnl=position.realized_pnl - position.costs_total,
    )


@router.get("/subscriptions/{subscription_id}/positions")
async def list_positions_endpoint(
    subscription_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _current_user: User = Depends(require_role),
) -> list[PaperPositionResponse]:
    """Every position the subscription has ever held a leg in (one for equity and futures, one per
    contract for an option strategy), flat ones included so their realized P&L and costs show.
    An open leg also carries its mark: the last price published for it, how old that is, and the
    unrealized P&L off it (all `None` if it has never been priced)."""
    if await db.get(PaperTradingSubscription, subscription_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Subscription not found")
    rows = (
        (
            await db.execute(
                select(PaperPosition)
                .where(PaperPosition.subscription_id == subscription_id)
                .order_by(PaperPosition.symbol)
            )
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC).timestamp()
    responses = []
    for position in rows:
        response = _position_response(position)
        if position.quantity != 0:
            tick = await get_latest_tick(redis, position.symbol)
            if tick is not None:
                response.mark_price = tick.price
                response.mark_age_seconds = max(0.0, now - tick.timestamp_ms / 1000.0)
                response.unrealized_pnl = (tick.price - position.avg_cost) * position.quantity
        responses.append(response)
    return responses


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
    return [_signal_response(s) for s in result.scalars().all()]


@router.post("/daily-signal-run", status_code=status.HTTP_201_CREATED)
async def run_daily_signal_endpoint(
    body: RunDailySignalRequest,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
    price_provider: DataLakePriceProvider = Depends(get_signal_price_provider),
) -> list[DailySignalResponse]:
    as_of = pd.Timestamp(body.as_of) if body.as_of else pd.Timestamp.now().normalize()
    signals = await run_daily_signal_generation(db, price_provider=price_provider, as_of=as_of)
    return [_signal_response(s) for s in signals]


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
