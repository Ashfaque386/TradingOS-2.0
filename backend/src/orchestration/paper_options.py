"""Options in paper trading: entering a structure, exiting it, stopping it out and squaring it off
before it expires (real-market-data plan, phase 5).

An option subscription reads signals from the underlying like a future, but what it holds is a set
of *legs*, one position per contract (`option_strategies` says which). Each underlying tick runs
`evaluate_option_subscription`:

1. **Holding legs.** A structure that has lost `stop_loss_pct` percent of what it cost is closed
  (the
   equity stop of 3% would be noise on a premium, so options subscriptions carry their own,
   default 50%). A signal that now points the other way closes it. Legs without a hedge -- a sold
   leg whose bought leg is gone -- are closed at once.
2. **Flat with a standing signal.** The legs are *chosen* from the broker's contract list and the
   spot price and kept as a short-lived plan in Redis (`paper:optplan:{subscription}`), so the tick
   job starts quoting them; once every leg has a fresh book the structure is *entered*. The two
   steps exist because a contract cannot be quoted before it is chosen.

How legs are executed
---------------------
- **Bought legs first, sold legs after, and a sold leg never exceeds what its hedge filled.** A
  spread
  whose bought leg fills only partly sells only that much; one whose bought leg does not fill
  sells nothing. The structure is therefore never naked, whatever the books allow.
- **Closing goes the other way:** sold legs are bought back before the bought leg they hedge is
  sold,
  and if a sold leg cannot be closed the bought legs are left alone.
- There is no atomicity across legs (as at an exchange without a combo order): each leg is its own
  fill, and an entry that did not complete is recorded (`paper_trading.option_entry_unbalanced`).
- The risk gate sees the structure as one intent, with its legs for the naked-options scan, so the
  kill switch, the scan and the compliance checks apply as for any order. The cash-equity notional
  limit and the circuit band do not (a premium may move far more in a day than a share's price may).
- What an entry commits is the premium it costs (its largest possible loss), judged against
  `PAPER_FUTURES_MAX_MARGIN_UTILISATION_PCT` of the subscription's capital.

The expiry sweep (`run_expiry_sweep`) squares a position off `PAPER_OPTIONS_SQUAREOFF_DAYS` days
before its
expiry, in market hours, through the normal close; a position that is still open after its expiry
(it could not be closed in time) is closed at the last real quote seen, as a cleanup, uncharged. A
future whose contract expired without being rolled is closed the same way. **Not modelled:**
exercise
and assignment, and physical delivery for stock F&O.
"""

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.brokers.base import BrokerAdapter, OptionContract, parse_option_symbol
from src.core.config import get_settings
from src.engine.options_grounding import GroundedLeg
from src.engine.paper_trading.depth import DepthUnavailableError
from src.engine.paper_trading.margin import check_max_loss_margin
from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.option_strategies import (
    EntryPlan,
    LegPlan,
    direction_for_signal,
    held_direction,
    max_loss,
    plan_entry,
    stop_triggered,
    structure_value,
)
from src.engine.paper_trading.order_book import OrderBookProvider, Side, book_for, simulate_fill
from src.engine.paper_trading.tick_feed import get_latest_tick
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.daily_signal import DailySignal
from src.models.data_source import DataSource, combine
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.engine_alerts import AlertCandidate, record_event_safely
from src.orchestration.paper_subscription_controls import (
    CloseBlockedError,
    PriceUnavailableError,
    SubscriptionStateError,
    close_position,
    open_positions,
)
from src.orchestration.paper_trading import _get_or_create_position, _persist_fill
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

logger = structlog.get_logger(__name__)

SWEEP_ACTOR = "system:expiry-sweep"
# A leg's price is only a mark (for the stop) if it is this fresh; the tick job refreshes it
# every few seconds.
MAX_MARK_AGE_SECONDS = 60.0

OptionContractsFn = Callable[[str], Awaitable[Sequence[OptionContract]]]
Action = Literal["none", "planned", "waiting", "entered", "refused", "exited", "exit_blocked"]


def plan_key(subscription_id: uuid.UUID) -> str:
    return f"paper:optplan:{subscription_id}"


def option_contracts_from(adapters: Callable[[], Sequence[BrokerAdapter]]) -> OptionContractsFn:
    """The contract list for an underlying from the first connected broker that has one; a broker
    that fails is skipped, and with none the list is empty (nothing is guessed)."""

    async def fetch(underlying: str) -> Sequence[OptionContract]:
        for adapter in adapters():
            try:
                contracts = await adapter.get_option_contracts(underlying)
            except Exception as exc:  # noqa: BLE001 - one broker failing must not stop the next
                logger.warning(
                    "paper_options.contracts_unavailable",
                    broker=adapter.broker_name,
                    underlying=underlying,
                    error=f"{type(exc).__name__}: {str(exc)[:120]}",
                )
                continue
            if contracts:
                return contracts
        return []

    return fetch


# ---- the plan: legs chosen, waiting for their books ----


@dataclass(frozen=True, slots=True)
class PlannedLeg:
    symbol: str
    side: Side
    quantity: int
    strike: float
    option_type: str


@dataclass(frozen=True, slots=True)
class StoredPlan:
    direction: str
    strategy: str
    expiry: str
    legs: tuple[PlannedLeg, ...]


async def save_plan(redis: Redis, subscription_id: uuid.UUID, plan: EntryPlan) -> None:
    body = {
        "direction": plan.direction,
        "strategy": plan.strategy,
        "expiry": plan.expiry.isoformat(),
        "legs": [
            {
                "symbol": leg.contract.symbol,
                "side": leg.side,
                "quantity": leg.quantity,
                "strike": leg.contract.strike,
                "option_type": leg.contract.option_type,
            }
            for leg in plan.legs
        ],
    }
    ttl_ms = max(1, int(get_settings().paper_options_plan_ttl_seconds * 1000))
    await redis.set(plan_key(subscription_id), json.dumps(body), px=ttl_ms)


async def load_plan(redis: Redis, subscription_id: uuid.UUID) -> StoredPlan | None:
    raw = await redis.get(plan_key(subscription_id))
    if raw is None:
        return None
    body = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
    return StoredPlan(
        direction=body["direction"],
        strategy=body["strategy"],
        expiry=body["expiry"],
        legs=tuple(
            PlannedLeg(
                symbol=leg["symbol"],
                side=leg["side"],
                quantity=int(leg["quantity"]),
                strike=float(leg["strike"]),
                option_type=leg["option_type"],
            )
            for leg in body["legs"]
        ),
    )


async def clear_plan(redis: Redis, subscription_id: uuid.UUID) -> None:
    await redis.delete(plan_key(subscription_id))


async def option_quote_symbols(db: AsyncSession, redis: Redis) -> set[str]:
    """What the tick job must quote for option subscriptions beyond their underlyings: every leg
    they hold, and every leg of a plan still waiting for its book."""
    subscriptions = (
        (
            await db.execute(
                select(PaperTradingSubscription).where(
                    PaperTradingSubscription.is_active.is_(True),
                    PaperTradingSubscription.instrument_kind == "option",
                )
            )
        )
        .scalars()
        .all()
    )
    if not subscriptions:
        return set()
    symbols: set[str] = set()
    held = await db.execute(
        select(PaperPosition.symbol).where(
            PaperPosition.subscription_id.in_([s.id for s in subscriptions]),
            PaperPosition.quantity != 0,
        )
    )
    symbols.update(row[0] for row in held.all())
    for subscription in subscriptions:
        plan = await load_plan(redis, subscription.id)
        if plan is not None:
            symbols.update(leg.symbol for leg in plan.legs)
    return symbols


# ---- evaluating a subscription on an underlying tick ----


@dataclass(frozen=True, slots=True)
class OptionEvaluation:
    action: Action
    fills: tuple[PaperFill, ...] = ()
    detail: str = ""


async def _latest_fill(db: AsyncSession, subscription_id: uuid.UUID) -> PaperFill:
    return (
        await db.execute(
            select(PaperFill)
            .where(PaperFill.subscription_id == subscription_id)
            .order_by(PaperFill.created_at.desc())
            .limit(1)
        )
    ).scalar_one()


async def _mark(redis: Redis, symbol: str, *, now: datetime) -> float | None:
    """A leg's price if it was quoted within `MAX_MARK_AGE_SECONDS`."""
    tick = await get_latest_tick(redis, symbol)
    if tick is None or now.timestamp() - tick.timestamp_ms / 1000.0 > MAX_MARK_AGE_SECONDS:
        return None
    return tick.price


async def evaluate_option_subscription(
    db: AsyncSession,
    redis: Redis,
    subscription_id: uuid.UUID,
    *,
    spot: float,
    tick_source: str = DataSource.UNKNOWN,
    contracts: OptionContractsFn | None,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    now: datetime | None = None,
) -> OptionEvaluation:
    """One underlying tick's worth of option logic for `subscription_id` (see the module doc)."""
    now = now or datetime.now(UTC)
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is None or not subscription.is_active or not subscription.is_option:
        return OptionEvaluation("none")

    held = await open_positions(db, subscription)
    signal = (
        await db.execute(
            select(DailySignal)
            .where(DailySignal.subscription_id == subscription.id)
            .order_by(DailySignal.generated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if held:
        if not any(leg.quantity > 0 for leg in held):
            return await _exit(
                db,
                redis,
                subscription,
                held,
                "a sold leg is left without its hedge",
                now=now,
                tick_source=tick_source,
                order_book_provider=order_book_provider,
                regulatory_provider=regulatory_provider,
            )
        marks = {leg.symbol: await _mark(redis, leg.symbol, now=now) for leg in held}
        if all(price is not None for price in marks.values()):
            cost = structure_value([(leg.quantity, leg.avg_cost) for leg in held])
            value = structure_value([(leg.quantity, marks[leg.symbol] or 0.0) for leg in held])
            if stop_triggered(
                risk=_held_risk_from(held, cost),
                loss=cost - value,
                stop_pct=subscription.stop_loss_pct,
            ):
                return await _exit(
                    db,
                    redis,
                    subscription,
                    held,
                    f"stop-loss ({subscription.stop_loss_pct:g}% of the premium)",
                    now=now,
                    tick_source=tick_source,
                    order_book_provider=order_book_provider,
                    regulatory_provider=regulatory_provider,
                )
        if signal is not None:
            direction = held_direction([(leg.symbol, leg.quantity) for leg in held])
            if direction is not None and direction != direction_for_signal(signal.signal_type):
                return await _exit(
                    db,
                    redis,
                    subscription,
                    held,
                    "the signal now points the other way",
                    now=now,
                    tick_source=tick_source,
                    order_book_provider=order_book_provider,
                    regulatory_provider=regulatory_provider,
                )
        return OptionEvaluation("none")

    if signal is None:
        return OptionEvaluation("none")
    wanted = direction_for_signal(signal.signal_type)
    stored = await load_plan(redis, subscription.id)
    if stored is not None and stored.direction == wanted:
        return await _enter(
            db,
            redis,
            subscription,
            stored,
            signal,
            now=now,
            tick_source=tick_source,
            order_book_provider=order_book_provider,
            regulatory_provider=regulatory_provider,
        )

    if contracts is None:
        logger.warning("paper_options.no_contract_source", subscription_id=str(subscription.id))
        return OptionEvaluation("none", detail="no contract source")
    listed = await contracts(subscription.symbol)
    settings = get_settings()
    plan = plan_entry(
        subscription.option_strategy or "long_option",
        signal.signal_type,
        spot=spot,
        contracts=listed,
        today=now.astimezone(IST).date(),
        min_days=settings.paper_options_min_days_to_expiry,
        lots=subscription.lots,
        width=subscription.spread_width,
    )
    if plan is None:
        logger.info(
            "paper_options.no_suitable_contract",
            subscription_id=str(subscription.id),
            underlying=subscription.symbol,
            spot=spot,
            listed=len(listed),
        )
        return OptionEvaluation("none", detail="no suitable contract is listed")
    await save_plan(redis, subscription.id, plan)
    logger.info(
        "paper_options.entry_planned",
        subscription_id=str(subscription.id),
        legs=[(leg.contract.symbol, leg.side, leg.quantity) for leg in plan.legs],
    )
    return OptionEvaluation("planned", detail=", ".join(leg.contract.symbol for leg in plan.legs))


def _held_risk_from(legs: list[PaperPosition], cost: float) -> float:
    """What the held legs can lose at most: their cost if all are bought, a credit spread's width
    less its credit otherwise (see `option_strategies.max_loss`)."""
    plan_legs = []
    for leg in legs:
        parsed = parse_option_symbol(leg.symbol)
        if parsed is None:
            continue
        underlying, expiry, strike, kind = parsed
        contract = OptionContract(leg.symbol, underlying, expiry, strike, kind, 1, 0.0)
        plan_legs.append(
            LegPlan(contract, "buy" if leg.quantity > 0 else "sell", abs(leg.quantity))
        )
    return max_loss(plan_legs, net_debit=cost)


async def _reference_price(redis: Redis, symbol: str) -> float:
    tick = await get_latest_tick(redis, symbol)
    return tick.price if tick is not None else 100.0  # only the synthetic book reads it


async def _enter(
    db: AsyncSession,
    redis: Redis,
    subscription: PaperTradingSubscription,
    plan: StoredPlan,
    signal: DailySignal,
    *,
    now: datetime,
    tick_source: str,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
) -> OptionEvaluation:
    """Opens the planned legs if every one has a usable book and the structure fits the account;
    otherwise waits (plan kept until it expires) or refuses (logged)."""
    books = {}
    for leg in plan.legs:
        try:
            books[leg.symbol] = await book_for(
                order_book_provider,
                leg.symbol,
                side=leg.side,
                mid_price=await _reference_price(redis, leg.symbol),
            )
        except DepthUnavailableError as exc:
            logger.info(
                "paper_options.entry_waiting_for_depth",
                subscription_id=str(subscription.id),
                symbol=leg.symbol,
                reason=str(exc),
            )
            return OptionEvaluation("waiting", detail=str(exc))

    rehearsal = {
        leg.symbol: simulate_fill(side=leg.side, quantity=leg.quantity, book=books[leg.symbol])
        for leg in plan.legs
    }
    bought = [leg for leg in plan.legs if leg.side == "buy"]
    if not bought or any(rehearsal[leg.symbol].filled_quantity == 0 for leg in bought):
        return OptionEvaluation("waiting", detail="no depth to buy the hedge against")
    debit = sum(
        (rehearsal[leg.symbol].avg_fill_price or 0.0)
        * rehearsal[leg.symbol].filled_quantity
        * (1 if leg.side == "buy" else -1)
        for leg in plan.legs
    )
    settings = get_settings()
    risk = max_loss(
        [
            LegPlan(
                OptionContract(
                    leg.symbol,
                    "",
                    date.fromisoformat(plan.expiry),
                    leg.strike,
                    leg.option_type,
                    1,
                    0.0,
                ),
                leg.side,
                leg.quantity,
            )
            for leg in plan.legs
        ],
        net_debit=debit,
    )
    margin = check_max_loss_margin(
        max_loss=risk,
        capital=subscription.initial_capital,
        max_utilisation_pct=settings.paper_futures_max_margin_utilisation_pct,
    )
    if not margin.ok:
        logger.info(
            "paper_options.entry_refused_for_margin",
            subscription_id=str(subscription.id),
            reason=margin.reason,
        )
        return OptionEvaluation("refused", detail=margin.reason or "")

    expiry_text = plan.expiry
    grounded = [
        GroundedLeg(
            option_type=leg.option_type,
            strike=leg.strike,
            expiry=expiry_text,
            action=leg.side,
            qty=leg.quantity,
            ltp=rehearsal[leg.symbol].avg_fill_price or 0.0,
            grounded=True,
        )
        for leg in plan.legs
    ]
    lead = bought[0]
    lead_price = rehearsal[lead.symbol].avg_fill_price or 0.0
    try:
        await create_order_intent(
            db,
            mode="paper",
            symbol=lead.symbol,
            side="buy",
            quantity=lead.quantity,
            proposed_price=lead_price,
            reference_price=lead_price,  # no circuit band on a premium: it is its own reference
            proposed_position_value=0.0,  # judged as premium above, not as notional
            portfolio_value=subscription.initial_capital,
            regulatory_provider=regulatory_provider,
            option_legs=grounded,
        )
    except KillSwitchTrippedError as exc:
        logger.warning(
            "paper_trading.blocked_by_kill_switch",
            subscription_id=str(subscription.id),
            symbol=lead.symbol,
            reason=exc.reason,
        )
        return OptionEvaluation("refused", detail=f"kill switch: {exc.reason}")
    except OrderIntentRejectedError as exc:
        logger.info(
            "paper_trading.order_intent_rejected",
            subscription_id=str(subscription.id),
            symbol=lead.symbol,
            reasons=exc.reasons,
        )
        return OptionEvaluation("refused", detail="; ".join(exc.reasons))

    source = combine([tick_source, getattr(order_book_provider, "data_source", DataSource.UNKNOWN)])
    group = uuid.uuid4()
    fills: list[PaperFill] = []
    hedge_filled = 0
    outcome: list[dict[str, object]] = []
    for index, leg in enumerate(plan.legs):
        quantity = leg.quantity
        if leg.side == "sell":
            quantity = min(quantity, hedge_filled)  # never sell more than the hedge bought
        if quantity <= 0:
            outcome.append(
                {"symbol": leg.symbol, "side": leg.side, "planned": leg.quantity, "filled": 0}
            )
            continue
        fill = simulate_fill(side=leg.side, quantity=quantity, book=books[leg.symbol])
        position = await _get_or_create_position(db, subscription, leg.symbol)
        await _persist_fill(
            db,
            subscription=subscription,
            position=position,
            side=leg.side,
            fill=fill,
            order_group_id=group,
            leg_index=index,
            data_source=source,
            symbol=leg.symbol,
        )
        fills.append(await _latest_fill(db, subscription.id))
        if leg.side == "buy":
            hedge_filled += fill.filled_quantity
        outcome.append(
            {
                "symbol": leg.symbol,
                "side": leg.side,
                "planned": leg.quantity,
                "filled": fill.filled_quantity,
            }
        )
    await clear_plan(redis, subscription.id)
    signal.consumed = True
    signal.consumed_at = now
    if any(entry["filled"] != entry["planned"] for entry in outcome):
        await write_audit_entry(
            db,
            actor="system:paper-options",
            action="paper_trading.option_entry_unbalanced",
            entity_type="paper_trading_subscription",
            entity_id=str(subscription.id),
            details={"strategy": plan.strategy, "legs": outcome},
        )
        logger.warning(
            "paper_options.entry_unbalanced", subscription_id=str(subscription.id), legs=outcome
        )
    await db.commit()
    return OptionEvaluation("entered", tuple(fills))


async def _exit(
    db: AsyncSession,
    redis: Redis,
    subscription: PaperTradingSubscription,
    held: list[PaperPosition],
    reason: str,
    *,
    now: datetime,
    tick_source: str,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
) -> OptionEvaluation:
    """Closes every held leg, sold legs first; stops at a sold leg that cannot be closed so the
    bought leg that hedges it is not sold out from under it."""
    source = combine([tick_source, getattr(order_book_provider, "data_source", DataSource.UNKNOWN)])
    group = uuid.uuid4()
    fills: list[PaperFill] = []
    for index, leg in enumerate(held):  # `open_positions` puts the sold legs first
        side: Side = "sell" if leg.quantity > 0 else "buy"
        price = await _mark(redis, leg.symbol, now=now)
        if price is None:
            price = leg.avg_cost
        try:
            book = await book_for(order_book_provider, leg.symbol, side=side, mid_price=price)
            await create_order_intent(
                db,
                mode="paper",
                symbol=leg.symbol,
                side=side,
                quantity=abs(leg.quantity),
                proposed_price=price,
                reference_price=price,
                proposed_position_value=0.0,
                portfolio_value=subscription.initial_capital,
                regulatory_provider=regulatory_provider,
            )
        except (DepthUnavailableError, KillSwitchTrippedError, OrderIntentRejectedError) as exc:
            logger.warning(
                "paper_options.exit_blocked",
                subscription_id=str(subscription.id),
                symbol=leg.symbol,
                reason=str(exc),
            )
            return OptionEvaluation("exit_blocked", tuple(fills), f"{leg.symbol}: {exc}")
        fill = simulate_fill(side=side, quantity=abs(leg.quantity), book=book)
        await _persist_fill(
            db,
            subscription=subscription,
            position=leg,
            side=side,
            fill=fill,
            order_group_id=group,
            leg_index=index,
            data_source=source,
            symbol=leg.symbol,
        )
        fills.append(await _latest_fill(db, subscription.id))
        if fill.remaining_quantity > 0 and leg.quantity < 0:
            break  # a sold leg only partly bought back: keep its hedge
    logger.info(
        "paper_options.exited",
        subscription_id=str(subscription.id),
        reason=reason,
        legs=[(f.symbol, f.side, f.filled_quantity) for f in fills],
    )
    if reason.startswith("stop-loss") and any(f.filled_quantity > 0 for f in fills):
        await record_event_safely(
            db,
            AlertCandidate(
                key=f"stop_loss:{group}",
                kind="stop_loss",
                severity="warning",
                title=f"{subscription.symbol}: option stop-loss hit, structure closed",
                detail=(
                    f"Closed {sum(1 for f in fills if f.filled_quantity > 0)} of {len(held)} "
                    f"legs: {reason}."
                ),
                subscription_id=subscription.id,
            ),
        )
    return OptionEvaluation("exited", tuple(fills), reason)


# ---- the expiry sweep ----


@dataclass(frozen=True, slots=True)
class SweepOutcome:
    subscription_id: str
    symbol: str
    status: Literal["closed", "postponed", "no_price"]
    detail: str = ""
    # Closed at the last quote after the contract had already expired, not squared off on time.
    forced: bool = False


def _expiry_of(subscription: PaperTradingSubscription, symbol: str) -> date | None:
    if subscription.is_option:
        parsed = parse_option_symbol(symbol)
        return parsed[1] if parsed is not None else None
    if subscription.is_future and symbol == subscription.contract_symbol:
        return subscription.contract_expiry
    return None


async def run_expiry_sweep(
    db: AsyncSession,
    redis: Redis,
    *,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    squareoff_days: int,
    now: datetime | None = None,
) -> list[SweepOutcome]:
    """Squares off option positions `squareoff_days` before expiry (normal close, so it needs a
    live tick and book and may be postponed), and closes at the last real quote any position left
    open after its expiry: an option past expiry, or a future whose contract was never rolled.
    Only active subscriptions are touched (an inactive one's open position is its operator's
    choice). A subscription's sold legs are handled first, and a sold leg that cannot be closed
    holds back the bought legs that hedge it."""
    today = (now or datetime.now(UTC)).astimezone(IST).date()
    subscriptions = (
        (
            await db.execute(
                select(PaperTradingSubscription).where(
                    PaperTradingSubscription.is_active.is_(True),
                    PaperTradingSubscription.instrument_kind.in_(("option", "future")),
                )
            )
        )
        .scalars()
        .all()
    )
    outcomes: list[SweepOutcome] = []
    for subscription in subscriptions:
        for position in await open_positions(db, subscription):
            expiry = _expiry_of(subscription, position.symbol)
            if expiry is None:
                continue
            days_left = (expiry - today).days
            if days_left < 0:
                outcome = await _close_at_last_quote(
                    db,
                    redis,
                    subscription,
                    position,
                    expiry,
                    order_book_provider,
                    regulatory_provider,
                    now,
                )
            elif subscription.is_option and days_left <= squareoff_days:
                outcome = await _close_before_expiry(
                    db, redis, subscription, position, order_book_provider, regulatory_provider, now
                )
            else:
                continue
            outcomes.append(outcome)
            if outcome.status != "closed" and position.quantity < 0:
                break  # keep the bought legs that hedge this one
    return outcomes


async def _close_before_expiry(
    db: AsyncSession,
    redis: Redis,
    subscription: PaperTradingSubscription,
    position: PaperPosition,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    now: datetime | None,
) -> SweepOutcome:
    try:
        result = await close_position(
            db,
            redis,
            subscription.id,
            actor=SWEEP_ACTOR,
            reason="squaring off before expiry",
            price=None,
            administrative=False,
            order_book_provider=order_book_provider,
            regulatory_provider=regulatory_provider,
            now=now,
            symbol=position.symbol,
        )
    except (CloseBlockedError, PriceUnavailableError, SubscriptionStateError) as exc:
        logger.warning("paper_options.squareoff_postponed", symbol=position.symbol, reason=str(exc))
        return SweepOutcome(str(subscription.id), position.symbol, "postponed", str(exc))
    if result.remaining_quantity != 0:
        return SweepOutcome(
            str(subscription.id),
            position.symbol,
            "postponed",
            f"only part closed ({result.remaining_quantity} left)",
        )
    return SweepOutcome(str(subscription.id), position.symbol, "closed")


async def _close_at_last_quote(
    db: AsyncSession,
    redis: Redis,
    subscription: PaperTradingSubscription,
    position: PaperPosition,
    expiry: date,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    now: datetime | None,
) -> SweepOutcome:
    tick = await get_latest_tick(redis, position.symbol)
    if tick is None:
        logger.warning("paper_options.expired_without_a_quote", symbol=position.symbol)
        return SweepOutcome(
            str(subscription.id), position.symbol, "no_price", "no quote was ever seen for it"
        )
    stamp = (now or datetime.now(UTC)).timestamp()
    age = max(0.0, stamp - tick.timestamp_ms / 1000.0)
    await close_position(
        db,
        redis,
        subscription.id,
        actor=SWEEP_ACTOR,
        reason=f"{position.symbol} expired on {expiry.isoformat()} with the position still open",
        price=tick.price,
        administrative=True,
        order_book_provider=order_book_provider,
        regulatory_provider=regulatory_provider,
        now=now,
        symbol=position.symbol,
        price_basis=(f"last real quote, {age / 3600:.1f}h old, at the expiry sweep", tick.source),
    )
    return SweepOutcome(
        str(subscription.id),
        position.symbol,
        "closed",
        f"at the last quote, {tick.price}",
        forced=True,
    )
