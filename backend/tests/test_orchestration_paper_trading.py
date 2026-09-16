"""Autonomous Paper Trading Engine orchestration tests (Build Spec §11):
Layer 1's daily signal generation, Layer 2's tick-driven entry/exit/
stop-loss/re-entry, and the fact that every action goes through Phase 6's
order-intent risk gate with no human approval anywhere in this module.
"""

import pandas as pd
from sqlalchemy import select

from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.paper_position import PaperPosition
from src.orchestration.kill_switch import check_drawdown
from src.orchestration.paper_trading import (
    enroll_in_paper_trading,
    process_tick,
    run_daily_signal_generation,
)
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"

_PRICE_PROVIDER = FakeDailyPriceProvider(seed_by_symbol={"DEMOSTOCK": 1})
_ORDER_BOOK_PROVIDER = MockOrderBookProvider()
_REGULATORY_PROVIDER = ReferenceTableRegulatoryDataProvider()

# Known from this fake provider's fixed seeded series (verified during
# development): 2026-09-09 is a genuine flat->long SMA-crossover BUY day.
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def _make_subscription(db_session_factory, **overrides):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="PaperDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        kwargs = dict(
            strategy_version_id=version.id,
            symbol="DEMOSTOCK",
            builtin_strategy="sma_crossover",
            sma_window=15,
            initial_capital=100_000.0,
            stop_loss_pct=3.0,
        )
        kwargs.update(overrides)
        subscription = await enroll_in_paper_trading(db, **kwargs)
    return subscription


async def test_enroll_persists_a_subscription(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    assert subscription.symbol == "DEMOSTOCK"
    assert subscription.is_active is True


async def test_daily_signal_generation_detects_a_real_transition(db_session_factory):
    subscription = await _make_subscription(db_session_factory)

    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )

    assert len(signals) == 1
    assert signals[0].subscription_id == subscription.id
    assert signals[0].signal_type == "BUY"
    assert signals[0].reference_price > 0


async def test_daily_signal_generation_skips_inactive_subscriptions(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="S", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="DEMOSTOCK",
            builtin_strategy="sma_crossover",
        )
        subscription.is_active = False
        await db.commit()

    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )
    assert signals == []


async def test_tick_enters_a_position_on_a_buy_signal(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )
    reference_price = signals[0].reference_price

    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=reference_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert fill is not None
    assert fill.side == "buy"
    assert fill.filled_quantity > 0

    async with db_session_factory() as db:
        result = await db.execute(
            select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
        )
        position = result.scalar_one()
    assert position.quantity == fill.filled_quantity
    assert position.avg_cost == fill.avg_fill_price


async def test_tick_with_no_signal_and_no_stop_loss_is_a_no_op(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=100.0,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    assert fill is None


async def test_stop_loss_flattens_the_position_with_a_realized_loss(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )
    reference_price = signals[0].reference_price

    async with db_session_factory() as db:
        entry_fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=reference_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    entry_price = entry_fill.avg_fill_price

    stop_price = entry_price * 0.9  # well past the 3% default stop
    async with db_session_factory() as db:
        exit_fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=stop_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert exit_fill is not None
    assert exit_fill.side == "sell"
    assert exit_fill.realized_pnl < 0

    async with db_session_factory() as db:
        result = await db.execute(
            select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
        )
        position = result.scalar_one()
    assert position.quantity == 0
    assert position.realized_pnl < 0


async def test_re_entry_after_a_stop_out_on_a_later_tick(db_session_factory):
    """The acceptance-critical Layer 2 behavior: a stopped-out position
    re-enters on a later tick without waiting for a new daily signal,
    because the latest signal is still BUY."""
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )
    reference_price = signals[0].reference_price

    async with db_session_factory() as db:
        entry_fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=reference_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    entry_price = entry_fill.avg_fill_price

    async with db_session_factory() as db:
        await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=entry_price * 0.9,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    async with db_session_factory() as db:
        re_entry_fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=entry_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert re_entry_fill is not None
    assert re_entry_fill.side == "buy"

    async with db_session_factory() as db:
        result = await db.execute(
            select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
        )
        position = result.scalar_one()
    assert position.quantity > 0


async def test_a_tripped_kill_switch_blocks_the_tick_from_producing_any_fill(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        signals = await run_daily_signal_generation(
            db, price_provider=_PRICE_PROVIDER, as_of=_BUY_AS_OF
        )
    reference_price = signals[0].reference_price

    async with db_session_factory() as db:
        await check_drawdown(db, "paper", current_equity=50_000, peak_equity=100_000)

    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=reference_price,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )

    assert fill is None, "a tripped paper kill switch must block the entry with no fill produced"

    async with db_session_factory() as db:
        result = await db.execute(
            select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
        )
        position = result.scalar_one_or_none()
    assert position is None or position.quantity == 0


async def test_process_tick_on_an_inactive_subscription_is_a_no_op(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    async with db_session_factory() as db:
        sub_row = await db.get(type(subscription), subscription.id)
        sub_row.is_active = False
        await db.commit()

    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=100.0,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    assert fill is None


async def test_process_tick_on_an_unknown_subscription_is_a_no_op(db_session_factory):
    import uuid

    async with db_session_factory() as db:
        fill = await process_tick(
            db,
            subscription_id=uuid.uuid4(),
            tick_price=100.0,
            order_book_provider=_ORDER_BOOK_PROVIDER,
            regulatory_provider=_REGULATORY_PROVIDER,
        )
    assert fill is None
