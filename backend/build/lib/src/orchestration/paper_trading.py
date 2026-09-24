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
from datetime import UTC, datetime

import pandas as pd
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.engine.backtest.engine import run_vectorized_backtest
from src.engine.backtest.signals import BuiltinStrategy, generate_builtin_signals
from src.engine.paper_trading.daily_signal import detect_todays_signal
from src.engine.paper_trading.order_book import FillResult, OrderBookProvider, Side, simulate_fill
from src.engine.paper_trading.position_ledger import Position, apply_fill
from src.engine.paper_trading.price_data import PriceDataProvider
from src.engine.paper_trading.stop_loss import check_universal_stop_loss
from src.engine.risk.compliance import RegulatoryDataProvider
from src.engine.risk.kill_switch import KillSwitchTrippedError
from src.models.daily_signal import DailySignal
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
) -> PaperTradingSubscription:
    subscription = PaperTradingSubscription(
        strategy_version_id=strategy_version_id,
        symbol=symbol,
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
        bars = price_provider.daily_bars(
            subscription.symbol, as_of=pd.Timestamp(as_of), lookback_days=DAILY_LOOKBACK_DAYS
        )
        signals = generate_builtin_signals(
            bars, subscription.builtin_strategy, subscription.sma_window
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
    db: AsyncSession, subscription: PaperTradingSubscription
) -> PaperPosition:
    result = await db.execute(
        select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
    )
    position = result.scalar_one_or_none()
    if position is None:
        position = PaperPosition(
            subscription_id=subscription.id, symbol=subscription.symbol, quantity=0, avg_cost=0.0
        )
        db.add(position)
        await db.commit()
        await db.refresh(position)
    return position


async def _persist_fill(
    db: AsyncSession,
    *,
    subscription: PaperTradingSubscription,
    position: PaperPosition,
    side: Side,
    fill: FillResult,
    order_group_id: uuid.UUID,
    leg_index: int = 0,
) -> PaperPosition:
    if fill.filled_quantity > 0:
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
            symbol=subscription.symbol,
            side=side,
            order_group_id=order_group_id,
            leg_index=leg_index,
            requested_quantity=fill.requested_quantity,
            filled_quantity=fill.filled_quantity,
            avg_fill_price=fill.avg_fill_price,
            fully_filled=fill.fully_filled,
            realized_pnl=realized,
        )
    )
    await db.commit()
    await db.refresh(position)
    return position


async def process_tick(
    db: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    tick_price: float,
    order_book_provider: OrderBookProvider,
    regulatory_provider: RegulatoryDataProvider,
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
            if position.quantity > 0 and latest_signal.signal_type == "SELL":
                side = "sell"
                quantity = position.quantity
                reference_price = latest_signal.reference_price
            elif position.quantity == 0 and latest_signal.signal_type == "BUY":
                side = "buy"
                entry_capital = subscription.initial_capital * (
                    subscription.position_size_pct / 100.0
                )
                quantity = int(entry_capital // tick_price)
                reference_price = latest_signal.reference_price

            if side is not None and not latest_signal.consumed:
                latest_signal.consumed = True
                latest_signal.consumed_at = datetime.now(UTC)

    if side is None or quantity <= 0:
        return None

    proposed_position_value = quantity * tick_price
    try:
        await create_order_intent(
            db,
            mode="paper",
            symbol=subscription.symbol,
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
            symbol=subscription.symbol,
            reason=exc.reason,
        )
        return None
    except OrderIntentRejectedError as exc:
        logger.info(
            "paper_trading.order_intent_rejected",
            subscription_id=str(subscription.id),
            symbol=subscription.symbol,
            reasons=exc.reasons,
        )
        return None

    book = order_book_provider.snapshot(subscription.symbol, mid_price=tick_price)
    fill = simulate_fill(side=side, quantity=quantity, book=book)

    await _persist_fill(
        db,
        subscription=subscription,
        position=position,
        side=side,
        fill=fill,
        order_group_id=uuid.uuid4(),
    )

    result = await db.execute(
        select(PaperFill)
        .where(PaperFill.subscription_id == subscription.id)
        .order_by(PaperFill.created_at.desc())
        .limit(1)
    )
    return result.scalar_one()
