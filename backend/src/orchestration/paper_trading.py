"""Autonomous Paper Trading Engine orchestration (Build Spec §11): wires
the pure engine pieces (src.engine.paper_trading.*) to persisted state and
to Phase 6's order-intent risk gate. **No function in this module ever
requires human approval** -- there is no `ApprovalGate`/
`conditional_transition` call anywhere here, unlike Phase 4's strategy
promotion. Every entry, exit, and re-entry goes through
`src.orchestration.risk_gate.create_order_intent(mode="paper", ...)`
first: that is this engine's only gate, and it's fully automatic (kill
switch / compliance / correlation), never a human click. A tripped kill
switch silently blocks the order intent -- including a stop-loss exit --
exactly as Phase 6 promises "nothing generates new order intents anywhere
in the system until reset," with no carve-out for exits.

**Layer 1** (`run_daily_signal_generation`) re-runs the trusted Phase 5
backtest engine once a day per active subscription and records whichever
BUY/SELL transition `src.engine.paper_trading.daily_signal` detects on
the latest bar.

**Layer 2** (`process_tick`) is driven by one tick at a time: it always
checks the universal stop-loss first (regardless of signal state); if not
stopped out, it acts on the subscription's *latest* daily signal -- exits
a long position on a SELL, and enters (or re-enters, after an earlier
stop-out the same day) a flat position on a BUY. Re-entry falls out of
this rule for free: a stop-loss exit only flattens the position, it
doesn't touch signal state, so the next tick's flat+BUY check fires again
on its own.
"""

import uuid
from dataclasses import fields
from datetime import UTC, datetime
from typing import cast

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.brokers.base import FuturesContract
from src.core.config import get_settings
from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.friction import (
    FrictionModel,
    compute_futures_leg_costs,
    compute_leg_costs,
    compute_option_leg_costs,
)
from src.engine.backtest.signals import BuiltinStrategy, generate_builtin_signals
from src.engine.paper_trading.daily_signal import detect_todays_signal
from src.engine.paper_trading.depth import DepthUnavailableError
from src.engine.paper_trading.margin import check_futures_margin
from src.engine.paper_trading.order_book import (
    FillResult,
    OrderBookProvider,
    Side,
    book_for,
    simulate_fill,
)
from src.engine.paper_trading.position_ledger import Position, apply_fill
from src.engine.paper_trading.price_data import InsufficientHistoryError, PriceDataProvider
from src.engine.paper_trading.stop_loss import check_universal_stop_loss
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.daily_signal import DailySignal
from src.models.data_source import DataSource, combine
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.risk_gate import OrderIntentRejectedError, create_order_intent

logger = structlog.get_logger(__name__)

DAILY_LOOKBACK_DAYS = 90

__all__ = [
    "enroll_in_paper_trading",
    "process_tick",
    "run_daily_signal_generation",
]


async def enroll_in_paper_trading(
    db: AsyncSession,
    *,
    strategy_version_id: uuid.UUID,
    symbol: str,
    builtin_strategy: BuiltinStrategy = "sma_crossover",
    sma_window: int = 15,
    initial_capital: float = 100_000.0,
    stop_loss_pct: float = 3.0,
    position_size_pct: float = 5.0,
    created_by: str | None = None,
    contract: FuturesContract | None = None,
    lots: int = 1,
    option_strategy: str | None = None,
    spread_width: int = 2,
) -> PaperTradingSubscription:
    """Enrols a strategy on `symbol`. With a `contract` the subscription trades that monthly
    future (`symbol` is then its underlying, which the signals are read from) in `lots` lots;
    with an `option_strategy` it holds the option legs that strategy chooses (see
    `src.engine.paper_trading.option_strategies`)."""
    if contract is not None and option_strategy is not None:
        raise ValueError("a subscription trades a future or an option strategy, not both")
    kind = "future" if contract is not None else "option" if option_strategy else "equity"
    subscription = PaperTradingSubscription(
        strategy_version_id=strategy_version_id,
        symbol=symbol,
        instrument_kind=kind,
        option_strategy=option_strategy,
        spread_width=spread_width,
        contract_symbol=contract.symbol if contract is not None else None,
        contract_expiry=contract.expiry if contract is not None else None,
        lot_size=contract.lot_size if contract is not None else None,
        lots=lots,
        builtin_strategy=builtin_strategy,
        sma_window=sma_window,
        initial_capital=initial_capital,
        stop_loss_pct=stop_loss_pct,
        position_size_pct=position_size_pct,
        created_by=created_by,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


async def run_daily_signal_generation(
    db: AsyncSession,
    *,
    price_provider: PriceDataProvider,
    as_of: pd.Timestamp,
) -> list[DailySignal]:
    result = await db.execute(
        select(PaperTradingSubscription).where(PaperTradingSubscription.is_active.is_(True))
    )
    subscriptions = result.scalars().all()

    created: list[DailySignal] = []
    for subscription in subscriptions:
        try:
            bars = price_provider.daily_bars(
                subscription.symbol, as_of=pd.Timestamp(as_of), lookback_days=DAILY_LOOKBACK_DAYS
            )
        except InsufficientHistoryError as exc:
            # Never substitute generated bars: this subscription waits for real history.
            logger.warning(
                "paper_trading.signal_skipped_awaiting_history",
                subscription_id=str(subscription.id),
                symbol=subscription.symbol,
                have=exc.have,
                need=exc.need,
            )
            continue
        # A real DB CHECK constraint (ck_paper_trading_subscriptions_builtin_strategy)
        # already restricts this column to exactly these two literal values --
        # the Mapped column itself is plain `str` since SQLAlchemy has no way
        # to express that constraint in its own type.
        signals = generate_builtin_signals(
            bars, cast(BuiltinStrategy, subscription.builtin_strategy), subscription.sma_window
        )

        # "Re-runs the full backtest once/day" (Build Spec §11): the real
        # Phase 5 engine runs here too, not just the raw signal -- its
        # result isn't itself the signal source (see this module's
        # docstring for why: the same position-lag rule the engine uses
        # internally is what detect_todays_signal reads directly, so both
        # are guaranteed consistent), but a genuinely failing backtest run
        # is worth surfacing rather than silently ignored.
        run_vectorized_backtest(bars, signals)

        decision = detect_todays_signal(signals)
        if decision.signal is None:
            continue

        signal = DailySignal(
            subscription_id=subscription.id,
            symbol=subscription.symbol,
            signal_type=decision.signal,
            reference_price=float(bars["close"].iloc[-1]),
        )
        db.add(signal)
        created.append(signal)

    if created:
        await db.commit()
        for signal in created:
            await db.refresh(signal)
    return created


async def _get_or_create_position(
    db: AsyncSession, subscription: PaperTradingSubscription, symbol: str | None = None
) -> PaperPosition:
    """The subscription's position in `symbol` (its trading symbol by default), created flat if
    it has none. An equity or futures subscription holds one; an option strategy one per leg."""
    symbol = symbol or subscription.trading_symbol
    result = await db.execute(
        select(PaperPosition).where(
            PaperPosition.subscription_id == subscription.id, PaperPosition.symbol == symbol
        )
    )
    position = result.scalar_one_or_none()
    if position is None:
        position = PaperPosition(
            subscription_id=subscription.id,
            symbol=symbol,
            quantity=0,
            avg_cost=0.0,
        )
        db.add(position)
        await db.commit()
        await db.refresh(position)
    return position


def friction_model() -> FrictionModel:
    """The cost rates in force: the published defaults, with any `PAPER_FRICTION_OVERRIDES`
    applied. An override naming a rate that does not exist is an error, not silently ignored --
    a typo must not leave the old rate in force while looking like a change."""
    overrides = get_settings().paper_friction_overrides
    unknown = set(overrides) - {f.name for f in fields(FrictionModel)}
    if unknown:
        raise ValueError(f"PAPER_FRICTION_OVERRIDES names no such rate: {sorted(unknown)}")
    return FrictionModel(**overrides)


def fill_costs(
    side: Side, fill: FillResult, *, instrument_kind: str = "equity"
) -> tuple[float, dict[str, float | str] | None]:
    """What the broker and the exchange would charge for this fill: the total, and each line
    (brokerage, STT, exchange charges, SEBI fee, stamp duty, GST) with the segment and product
    they were worked out for. Nothing for a fill that filled nothing. The depth walk already
    prices the market impact of the order, so no further slippage is added on top."""
    if fill.filled_quantity == 0 or fill.avg_fill_price is None:
        return 0.0, None
    turnover = fill.filled_quantity * fill.avg_fill_price
    model = friction_model()
    if instrument_kind == "future":
        product = "nrml"  # a future is carried overnight on margin
        legs = compute_futures_leg_costs(turnover=turnover, is_buy=side == "buy", model=model)
    elif instrument_kind == "option":
        product = "nrml"
        # `turnover` is the premium traded here, which is what the option charges are levied on.
        legs = compute_option_leg_costs(turnover=turnover, is_buy=side == "buy", model=model)
    else:
        product = get_settings().paper_trading_product
        legs = compute_leg_costs(
            turnover=turnover, is_buy=side == "buy", is_delivery=product == "cnc", model=model
        )
    return legs.total, {
        "segment": instrument_kind,
        "product": product,
        "turnover": turnover,
        "brokerage": legs.brokerage,
        "stt": legs.stt,
        "exchange_txn_charges": legs.exchange_txn_charges,
        "sebi_fee": legs.sebi_fee,
        "stamp_duty": legs.stamp_duty,
        "gst": legs.gst,
    }


async def _persist_fill(
    db: AsyncSession,
    *,
    subscription: PaperTradingSubscription,
    position: PaperPosition,
    side: Side,
    fill: FillResult,
    order_group_id: uuid.UUID,
    leg_index: int = 0,
    data_source: str = DataSource.UNKNOWN,
    charge_costs: bool = True,
    symbol: str | None = None,
) -> PaperPosition:
    costs, breakdown = (
        fill_costs(side, fill, instrument_kind=subscription.instrument_kind)
        if charge_costs
        else (0.0, None)
    )
    position.costs_total += costs
    if fill.filled_quantity > 0:
        # FillResult.avg_fill_price is None only when nothing filled at all
        # (see its own field comment) -- filled_quantity > 0 here rules
        # that out.
        assert fill.avg_fill_price is not None
        application = apply_fill(
            Position(quantity=position.quantity, avg_cost=position.avg_cost),
            side=side,
            fill_quantity=fill.filled_quantity,
            fill_price=fill.avg_fill_price,
        )
        position.quantity = application.new_position.quantity
        position.avg_cost = application.new_position.avg_cost
        position.realized_pnl += application.realized_pnl
        realized = application.realized_pnl
    else:
        realized = 0.0

    db.add(
        PaperFill(
            subscription_id=subscription.id,
            symbol=symbol or position.symbol,
            side=side,
            order_group_id=order_group_id,
            leg_index=leg_index,
            requested_quantity=fill.requested_quantity,
            filled_quantity=fill.filled_quantity,
            avg_fill_price=fill.avg_fill_price,
            fully_filled=fill.fully_filled,
            realized_pnl=realized,
            costs_total=costs,
            costs_breakdown=breakdown,
            data_source=data_source,
        )
    )
    await db.commit()
    await db.refresh(position)
    return position


def decide_order(
    subscription: PaperTradingSubscription,
    *,
    position_quantity: int,
    signal_type: str,
    tick_price: float,
) -> tuple[Side, int] | None:
    """The order the latest signal calls for given the position, or `None`.

    Equity is long-only: a BUY opens (sized as a share of capital), a SELL closes. A future goes
    both ways, in whole lots: BUY opens a long or covers a short, SELL opens a short or sells a
    long out. A flip is an exit and then, on the next tick, the entry the same signal calls for
    (the same "re-entry falls out for free" rule the equity path has)."""
    if subscription.is_future:
        lots_units = subscription.lots * (subscription.lot_size or 0)
        if position_quantity > 0 and signal_type == "SELL":
            return "sell", position_quantity
        if position_quantity < 0 and signal_type == "BUY":
            return "buy", -position_quantity
        if position_quantity == 0 and lots_units > 0:
            return ("buy" if signal_type == "BUY" else "sell"), lots_units
        return None
    if position_quantity > 0 and signal_type == "SELL":
        return "sell", position_quantity
    if position_quantity == 0 and signal_type == "BUY":
        entry_capital = subscription.initial_capital * (subscription.position_size_pct / 100.0)
        return "buy", int(entry_capital // tick_price)
    return None


async def process_tick(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    tick_price: float,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
    tick_source: str = DataSource.UNKNOWN,
) -> PaperFill | None:
    """Returns the `PaperFill` this tick produced, or `None` if the tick
    triggered no action. Raises nothing on its own for a risk-gate
    rejection -- that's an expected, logged-and-swallowed outcome here
    (the whole point of the gate is to sometimes say no autonomously),
    never a crash."""
    subscription = await db.get(PaperTradingSubscription, subscription_id)
    if subscription is None or not subscription.is_active:
        return None

    position = await _get_or_create_position(db, subscription)

    stop = check_universal_stop_loss(
        position_qty=position.quantity,
        avg_cost=position.avg_cost,
        tick_price=tick_price,
        stop_loss_pct=subscription.stop_loss_pct,
    )

    side: Side | None = None
    quantity = 0
    signal_to_consume: DailySignal | None = None
    reference_price = position.avg_cost if position.avg_cost > 0 else tick_price

    if stop.triggered:
        side = "sell" if position.quantity > 0 else "buy"
        quantity = abs(position.quantity)
    else:
        latest_signal_result = await db.execute(
            select(DailySignal)
            .where(DailySignal.subscription_id == subscription.id)
            .order_by(DailySignal.generated_at.desc())
            .limit(1)
        )
        latest_signal = latest_signal_result.scalar_one_or_none()

        if latest_signal is not None:
            decided = decide_order(
                subscription,
                position_quantity=position.quantity,
                signal_type=latest_signal.signal_type,
                tick_price=tick_price,
            )
            if decided is not None:
                side, quantity = decided
                reference_price = latest_signal.reference_price
                signal_to_consume = latest_signal

    if side is None or quantity <= 0:
        return None

    # The book first: an order that cannot be priced against real depth (no snapshot, a stale one)
    # waits -- the signal stays pending and the next tick tries again -- instead of being consumed
    # by a fill that did not happen.
    try:
        book = await book_for(
            order_book_provider, subscription.trading_symbol, side=side, mid_price=tick_price
        )
    except DepthUnavailableError as exc:
        logger.warning(
            "paper_trading.order_waiting_for_depth",
            subscription_id=str(subscription.id),
            symbol=subscription.trading_symbol,
            side=side,
            reason=str(exc),
        )
        return None

    if signal_to_consume is not None and not signal_to_consume.consumed:
        signal_to_consume.consumed = True
        signal_to_consume.consumed_at = datetime.now(UTC)

    proposed_position_value = quantity * tick_price
    opens_a_position = position.quantity == 0
    if subscription.is_future:
        # A future is bought on margin: the cash-equity "notional against capital" limit would
        # refuse almost any index future on a small account, so what an entry commits is judged
        # as margin (an estimate, labelled so), and the gate's notional limit is not applied.
        # Closing a position needs no margin.
        proposed_position_value = 0.0
        if opens_a_position:
            settings = get_settings()
            margin = check_futures_margin(
                price=tick_price,
                quantity=quantity,
                capital=subscription.initial_capital,
                margin_pct=settings.paper_futures_margin_pct,
                max_utilisation_pct=settings.paper_futures_max_margin_utilisation_pct,
            )
            if not margin.ok:
                logger.info(
                    "paper_trading.order_refused_for_margin",
                    subscription_id=str(subscription.id),
                    symbol=subscription.trading_symbol,
                    reason=margin.reason,
                )
                return None
    try:
        await create_order_intent(
            db,
            mode="paper",
            symbol=subscription.trading_symbol,
            side=side,
            quantity=quantity,
            proposed_price=tick_price,
            reference_price=reference_price,
            proposed_position_value=proposed_position_value,
            portfolio_value=subscription.initial_capital,
            regulatory_provider=regulatory_provider,
        )
    except KillSwitchTrippedError as exc:
        # The load-bearing Phase 6 guarantee: a tripped switch blocks this
        # order intent same as any other -- logged distinctly from a
        # routine compliance rejection so an operator can tell "the
        # engine is fully halted" from "one order was rejected," but
        # never raised further: an uncaught exception here would kill the
        # intraday drain job for every other subscription's tick too,
        # which is the opposite of "runs autonomously."
        logger.warning(
            "paper_trading.blocked_by_kill_switch",
            subscription_id=str(subscription.id),
            symbol=subscription.trading_symbol,
            reason=exc.reason,
        )
        return None
    except OrderIntentRejectedError as exc:
        logger.info(
            "paper_trading.order_intent_rejected",
            subscription_id=str(subscription.id),
            symbol=subscription.trading_symbol,
            reasons=exc.reasons,
        )
        return None

    fill = simulate_fill(side=side, quantity=quantity, book=book)

    await _persist_fill(
        db,
        subscription=subscription,
        position=position,
        side=side,
        fill=fill,
        order_group_id=uuid.uuid4(),
        # The fill rests on the tick price AND the order book it was walked through.
        data_source=combine(
            [tick_source, getattr(order_book_provider, "data_source", DataSource.UNKNOWN)]
        ),
    )

    result = await db.execute(
        select(PaperFill)
        .where(PaperFill.subscription_id == subscription.id)
        .order_by(PaperFill.created_at.desc())
        .limit(1)
    )
    return result.scalar_one()
