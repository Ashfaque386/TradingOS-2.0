"""Operator controls for paper-trading subscriptions: list, deactivate, activate, close a position.

Until now a subscription could be enrolled and read but never stopped: `is_active` was only set at
enrolment, so stopping one (and closing what it had open) meant editing the database. Doing that to
a subscription with an open position also left the position **unmanaged** -- `process_tick` ignores
an inactive subscription, so nothing watches its stop-loss -- while the dashboards kept counting it.
These functions make each step explicit and recorded:

- **deactivate** refuses while a position is open unless the operator says what to do with it:
  `close` it first, or `leave_unmanaged` (an acknowledgement that the stop-loss stops, not a
  default).
- **activate** puts the tick cursor at the newest tick on the symbol's stream. A subscription
  that was off for hours would otherwise drain -- and act on, and feed the shared latency guard
  with -- every tick published while it was off.
- **close_position** closes at a fresh price. By default it goes through the same risk gate and
  kill switch as any other order (no carve-out for exits: that is the project's rule) and walks the
  order book, so it can fill partly. `administrative=True` (SystemAdministrator only) skips the
  gate and fills the whole quantity at the given price; it exists for stale or demo cleanup and is
  audited as such.

A price is never invented: a manual close uses a tick no older than `MAX_TICK_AGE_SECONDS`, or the
price the operator supplies (labelled `supplied`). When the market is shut the newest tick is
stale, so the operator must supply one.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import pandas as pd
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.tick_source import feed_unavailable_reason
from src.engine.paper_trading.depth import DepthUnavailableError
from src.engine.paper_trading.order_book import (
    FillResult,
    OrderBookProvider,
    Side,
    book_for,
    simulate_fill,
)
from src.engine.paper_trading.price_data import InsufficientHistoryError, PriceDataProvider
from src.engine.paper_trading.tick_feed import Tick, get_latest_tick, get_latest_tick_id
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.data_source import DataSource, combine
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_trading import _get_or_create_position, _persist_fill
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

logger = structlog.get_logger(__name__)

# A manual close will not price off a tick older than this: a stale price is not a price.
MAX_TICK_AGE_SECONDS = 120.0
HISTORY_LOOKBACK_DAYS = 30

OnOpenPosition = Literal["close", "leave_unmanaged"]
SubscriptionState = Literal["active", "awaiting_history", "halted_no_data", "inactive"]


class SubscriptionNotFoundError(Exception):
    pass


class SubscriptionStateError(Exception):
    """The request would change nothing, or conflicts with the subscription's state (already
    inactive, no open position, a duplicate...). Maps to HTTP 409."""


class CloseBlockedError(SubscriptionStateError):
    """The kill switch or the risk gate refused the closing order, or there was nothing to fill
    against. `reasons` are the gate's own words."""

    def __init__(self, message: str, reasons: list[str] | None = None):
        super().__init__(message)
        self.reasons = reasons or []


class PriceUnavailableError(Exception):
    """No fresh price to close at and none supplied. Maps to HTTP 422."""


@dataclass(frozen=True, slots=True)
class CloseResult:
    fill: PaperFill
    position: PaperPosition
    remaining_quantity: int
    price: float
    price_basis: str
    data_source: str
    administrative: bool


@dataclass(frozen=True, slots=True)
class DeactivationResult:
    subscription: PaperTradingSubscription
    closed: CloseResult | None
    left_unmanaged_quantity: int = 0


@dataclass(frozen=True, slots=True)
class SubscriptionView:
    subscription: PaperTradingSubscription
    state: SubscriptionState
    state_reason: str | None
    quantity: int
    avg_cost: float
    realized_pnl: float
    costs_total: float
    last_tick: Tick | None = None
    last_tick_age_seconds: float | None = None
    unrealized_pnl: float | None = None


# ---- state ----


def derive_state(
    *, is_active: bool, history_problem: str | None, feed_problem: str | None
) -> tuple[SubscriptionState, str | None]:
    """The one rule for what to call a subscription. Order matters: an inactive subscription is
    inactive whatever else is wrong; one that cannot get history cannot signal; one with no feed
    cannot be priced."""
    if not is_active:
        return "inactive", None
    if history_problem is not None:
        return "awaiting_history", history_problem
    if feed_problem is not None:
        return "halted_no_data", feed_problem
    return "active", None


def _history_problem(price_provider: PriceDataProvider, symbol: str, as_of: datetime) -> str | None:
    try:
        price_provider.daily_bars(
            symbol, as_of=pd.Timestamp(as_of.date()), lookback_days=HISTORY_LOOKBACK_DAYS
        )
    except InsufficientHistoryError as exc:
        return str(exc)
    return None


async def list_subscription_views(
    db: AsyncSession,
    redis: Redis,
    price_provider: PriceDataProvider,
    *,
    active: bool | None = None,
    symbol: str | None = None,
    state: SubscriptionState | None = None,
    now: datetime | None = None,
) -> list[SubscriptionView]:
    now = now or datetime.now(UTC)
    query = select(PaperTradingSubscription).order_by(PaperTradingSubscription.created_at.desc())
    if active is not None:
        query = query.where(PaperTradingSubscription.is_active.is_(active))
    if symbol:
        query = query.where(PaperTradingSubscription.symbol == symbol.upper())
    subscriptions = (await db.execute(query)).scalars().all()
    positions = {
        p.subscription_id: p
        for p in (
            await db.execute(
                select(PaperPosition).where(
                    PaperPosition.subscription_id.in_([s.id for s in subscriptions])
                )
            )
        )
        .scalars()
        .all()
    }
    feed_problem = feed_unavailable_reason()

    views: list[SubscriptionView] = []
    for subscription in subscriptions:
        history_problem = (
            _history_problem(price_provider, subscription.symbol, now)
            if subscription.is_active
            else None
        )
        derived, reason = derive_state(
            is_active=subscription.is_active,
            history_problem=history_problem,
            feed_problem=feed_problem,
        )
        if state is not None and derived != state:
            continue
        position = positions.get(subscription.id)
        quantity = position.quantity if position else 0
        avg_cost = position.avg_cost if position else 0.0
        tick = await get_latest_tick(redis, subscription.symbol)
        age = max(0.0, now.timestamp() - tick.timestamp_ms / 1000.0) if tick else None
        unrealized = (tick.price - avg_cost) * quantity if tick and quantity != 0 else None
        views.append(
            SubscriptionView(
                subscription=subscription,
                state=derived,
                state_reason=reason,
                quantity=quantity,
                avg_cost=avg_cost,
                realized_pnl=position.realized_pnl if position else 0.0,
                costs_total=position.costs_total if position else 0.0,
                last_tick=tick,
                last_tick_age_seconds=age,
                unrealized_pnl=unrealized,
            )
        )
    return views


# ---- helpers ----


async def _load(db: AsyncSession, subscription_id: uuid.UUID) -> PaperTradingSubscription:
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is None:
        raise SubscriptionNotFoundError(str(subscription_id))
    return subscription


async def assert_no_active_duplicate(
    db: AsyncSession, *, strategy_version_id: uuid.UUID, symbol: str
) -> None:
    """At most one active subscription per (strategy version, symbol): two would trade the same
    symbol twice under one strategy and double-count its position and P&L."""
    query = select(PaperTradingSubscription.id).where(
        PaperTradingSubscription.strategy_version_id == strategy_version_id,
        PaperTradingSubscription.symbol == symbol,
        PaperTradingSubscription.is_active.is_(True),
    )
    if (await db.execute(query)).first() is not None:
        raise SubscriptionStateError(
            f"an active paper-trading subscription for {symbol} already exists for this strategy "
            "version; deactivate it first"
        )


async def _price_for_close(
    redis: Redis, symbol: str, supplied: float | None, now: datetime
) -> tuple[float, str, str]:
    """(price, basis, data_source label). Supplied wins; otherwise a fresh tick; otherwise an
    error that tells the operator what to do."""
    if supplied is not None:
        return supplied, "supplied by the operator", DataSource.SUPPLIED
    tick = await get_latest_tick(redis, symbol)
    if tick is None:
        raise PriceUnavailableError(
            f"no price has ever been published for {symbol}; supply `price` to close at"
        )
    age = now.timestamp() - tick.timestamp_ms / 1000.0
    if age > MAX_TICK_AGE_SECONDS:
        raise PriceUnavailableError(
            f"the newest {symbol} price is {age:.0f}s old (limit {MAX_TICK_AGE_SECONDS:.0f}s; the "
            "market may be shut); supply `price` to close at"
        )
    return tick.price, f"live tick ({age:.0f}s old)", tick.source


# ---- close ----


async def close_position(
    db: AsyncSession,
    redis: Redis,
    subscription_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    price: float | None,
    administrative: bool,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    now: datetime | None = None,
) -> CloseResult:
    now = now or datetime.now(UTC)
    subscription = await _load(db, subscription_id)
    position = await _get_or_create_position(db, subscription)
    if position.quantity == 0:
        raise SubscriptionStateError(f"{subscription.symbol} has no open position to close")

    quantity = abs(position.quantity)
    side: Side = "sell" if position.quantity > 0 else "buy"
    close_price, basis, label = await _price_for_close(redis, subscription.symbol, price, now)

    if administrative:
        # Whole quantity at the given price, no gate, no book: the rule for cleanup, not trading.
        fill = FillResult(
            requested_quantity=quantity,
            filled_quantity=quantity,
            remaining_quantity=0,
            avg_fill_price=close_price,
            fully_filled=True,
        )
        data_source = label
    else:
        try:
            await create_order_intent(
                db,
                mode="paper",
                symbol=subscription.symbol,
                side=side,
                quantity=quantity,
                proposed_price=close_price,
                reference_price=position.avg_cost if position.avg_cost > 0 else close_price,
                proposed_position_value=quantity * close_price,
                portfolio_value=subscription.initial_capital,
                regulatory_provider=regulatory_provider,
            )
        except KillSwitchTrippedError as exc:
            raise CloseBlockedError(
                f"the paper kill switch is tripped: {exc.reason}", [str(exc.reason)]
            ) from exc
        except OrderIntentRejectedError as exc:
            raise CloseBlockedError(
                "the risk gate refused the closing order: " + "; ".join(exc.reasons),
                list(exc.reasons),
            ) from exc
        try:
            book = await book_for(
                order_book_provider, subscription.symbol, side=side, mid_price=close_price
            )
        except DepthUnavailableError as exc:
            raise CloseBlockedError(f"no live order book to fill the closing order: {exc}") from exc
        fill = simulate_fill(side=side, quantity=quantity, book=book)
        if fill.filled_quantity == 0:
            raise CloseBlockedError("there is no depth to fill the closing order against")
        data_source = combine(
            [label, getattr(order_book_provider, "data_source", DataSource.UNKNOWN)]
        )

    before = position.realized_pnl
    await _persist_fill(
        db,
        subscription=subscription,
        position=position,
        side=side,
        fill=fill,
        order_group_id=uuid.uuid4(),
        data_source=data_source,
        charge_costs=not administrative,  # a cleanup is not a trade: no broker would charge it
    )
    latest = (
        await db.execute(
            select(PaperFill)
            .where(PaperFill.subscription_id == subscription.id)
            .order_by(PaperFill.created_at.desc())
            .limit(1)
        )
    ).scalar_one()
    await write_audit_entry(
        db,
        actor=actor,
        action="paper_trading.position_closed",
        entity_type="paper_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "symbol": subscription.symbol,
            "side": side,
            "requested_quantity": quantity,
            "filled_quantity": fill.filled_quantity,
            "price": fill.avg_fill_price,
            "price_basis": basis,
            "data_source": data_source,
            "administrative": administrative,
            "reason": reason,
            "realized_pnl_before": before,
            "realized_pnl_after": position.realized_pnl,
        },
    )
    await db.commit()
    await db.refresh(position)
    logger.info(
        "paper_trading.position_closed",
        subscription_id=str(subscription.id),
        symbol=subscription.symbol,
        administrative=administrative,
        remaining=fill.remaining_quantity,
    )
    return CloseResult(
        fill=latest,
        position=position,
        remaining_quantity=fill.remaining_quantity,
        price=float(fill.avg_fill_price or close_price),
        price_basis=basis,
        data_source=data_source,
        administrative=administrative,
    )


# ---- deactivate / activate ----


async def deactivate_subscription(
    db: AsyncSession,
    redis: Redis,
    subscription_id: uuid.UUID,
    *,
    actor: str,
    reason: str,
    on_open_position: OnOpenPosition | None,
    close_price: float | None,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    now: datetime | None = None,
) -> DeactivationResult:
    now = now or datetime.now(UTC)
    subscription = await _load(db, subscription_id)
    if not subscription.is_active:
        raise SubscriptionStateError(f"the {subscription.symbol} subscription is already inactive")

    position = await _get_or_create_position(db, subscription)
    closed: CloseResult | None = None
    if position.quantity != 0:
        if on_open_position is None:
            raise SubscriptionStateError(
                f"{subscription.symbol} has an open position of {position.quantity}: deactivating "
                "would leave it unmanaged (no stop-loss). Say `on_open_position`: 'close' to close "
                "it first, or 'leave_unmanaged' to accept that."
            )
        if on_open_position == "close":
            closed = await close_position(
                db,
                redis,
                subscription_id,
                actor=actor,
                reason=f"closing before deactivation: {reason}",
                price=close_price,
                administrative=False,
                order_book_provider=order_book_provider,
                regulatory_provider=regulatory_provider,
                now=now,
            )
            if closed.remaining_quantity > 0:
                raise SubscriptionStateError(
                    f"the position was only partly closed ({closed.remaining_quantity} left); the "
                    "subscription stays active -- close the rest, then deactivate"
                )
            subscription = await _load(db, subscription_id)

    left_unmanaged = position.quantity if closed is None else 0
    subscription.is_active = False
    subscription.deactivated_at = now
    subscription.deactivated_by = actor
    subscription.deactivation_reason = reason
    await write_audit_entry(
        db,
        actor=actor,
        action="paper_trading.subscription_deactivated",
        entity_type="paper_trading_subscription",
        entity_id=str(subscription.id),
        details={
            "symbol": subscription.symbol,
            "reason": reason,
            "on_open_position": on_open_position,
            "closed_quantity": closed.fill.filled_quantity if closed else 0,
            "left_unmanaged_quantity": left_unmanaged,
        },
    )
    await db.commit()
    await db.refresh(subscription)
    return DeactivationResult(
        subscription=subscription, closed=closed, left_unmanaged_quantity=left_unmanaged
    )


async def activate_subscription(
    db: AsyncSession, redis: Redis, subscription_id: uuid.UUID, *, actor: str
) -> PaperTradingSubscription:
    subscription = await _load(db, subscription_id)
    if subscription.is_active:
        raise SubscriptionStateError(f"the {subscription.symbol} subscription is already active")
    # (This subscription is inactive here, so it cannot match its own duplicate check.)
    await assert_no_active_duplicate(
        db, strategy_version_id=subscription.strategy_version_id, symbol=subscription.symbol
    )
    # Start at "now": ticks published while it was off are history, not instructions.
    subscription.tick_cursor = await get_latest_tick_id(redis, subscription.symbol) or "0"
    subscription.is_active = True
    subscription.deactivated_at = None
    subscription.deactivated_by = None
    subscription.deactivation_reason = None
    await write_audit_entry(
        db,
        actor=actor,
        action="paper_trading.subscription_activated",
        entity_type="paper_trading_subscription",
        entity_id=str(subscription.id),
        details={"symbol": subscription.symbol, "tick_cursor": subscription.tick_cursor},
    )
    await db.commit()
    await db.refresh(subscription)
    return subscription
