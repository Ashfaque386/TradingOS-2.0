"""Paper fills on the broker's real depth, with the Indian cost model (phase 3).

An order walks the live book level by level; with no fresh book it waits (the signal stays
pending); and every fill carries what it would have cost -- brokerage, STT, exchange charges, SEBI
fee, stamp duty, GST -- kept apart from the gross realized result.
"""

import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.core.config import get_settings
from src.engine.paper_trading.depth import (
    DepthSnapshot,
    RealDepthOrderBookProvider,
    depth_key,
    publish_depth,
)
from src.engine.paper_trading.order_book import OrderBookLevel
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.models.daily_signal import DailySignal
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.orchestration.paper_trading import enroll_in_paper_trading, process_tick
from src.orchestration.strategies import create_strategy, create_version_with_validation

SYMBOL = "REALDEPTH"
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


def _book(*, asks=((100.1, 30), (100.2, 400)), bids=((100.0, 50), (99.9, 200)), age=0.0):
    return DepthSnapshot(
        bids=tuple(OrderBookLevel(p, q) for p, q in bids),
        asks=tuple(OrderBookLevel(p, q) for p, q in asks),
        timestamp_ms=int((time.time() - age) * 1000),
    )


@pytest_asyncio.fixture
async def redis(redis_client):
    await redis_client.delete(depth_key(SYMBOL))
    yield redis_client
    await redis_client.delete(depth_key(SYMBOL))


@pytest.fixture(autouse=True)
def _settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _subscription(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"RD-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol=SYMBOL,
            initial_capital=100_000.0,
            position_size_pct=5.0,  # Rs 5,000 at ~100 is 50 shares
            stop_loss_pct=3.0,
        )


async def _signal(db_session_factory, subscription, kind: str, reference: float = 100.0):
    async with db_session_factory() as db:
        signal = DailySignal(
            subscription_id=subscription.id,
            symbol=SYMBOL,
            signal_type=kind,
            reference_price=reference,
        )
        db.add(signal)
        await db.commit()
        return signal.id


async def _tick(db_session_factory, redis, subscription, price: float = 100.05):
    async with db_session_factory() as db:
        return await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=price,
            tick_source="real",
            order_book_provider=RealDepthOrderBookProvider(redis, max_age_seconds=30),
            regulatory_provider=_REGULATORY,
        )


async def _position(db_session_factory, subscription) -> PaperPosition:
    async with db_session_factory() as db:
        return (
            await db.execute(
                select(PaperPosition).where(PaperPosition.subscription_id == subscription.id)
            )
        ).scalar_one()


async def _consumed(db_session_factory, signal_id) -> bool:
    async with db_session_factory() as db:
        return (await db.get(DailySignal, signal_id)).consumed


async def _fills(db_session_factory, subscription) -> list[PaperFill]:
    async with db_session_factory() as db:
        return list(
            (
                await db.execute(
                    select(PaperFill)
                    .where(PaperFill.subscription_id == subscription.id)
                    .order_by(PaperFill.created_at)
                )
            )
            .scalars()
            .all()
        )


# ---- walking the real book ----


async def test_an_entry_walks_the_real_asks_and_is_labelled_real(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    # 5,000 / 100.05 = 49 shares: 30 @ 100.1, then 19 @ 100.2
    assert fill is not None and fill.filled_quantity == 49 and fill.fully_filled
    assert fill.avg_fill_price == pytest.approx((30 * 100.1 + 19 * 100.2) / 49)
    assert fill.data_source == "real"  # a real tick on a real book
    position = await _position(db_session_factory, subscription)
    assert position.quantity == 49 and position.avg_cost == pytest.approx(fill.avg_fill_price)


async def test_a_thin_book_fills_part_of_the_order_and_charges_only_for_that(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(asks=((100.1, 10),)), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill.filled_quantity == 10 and not fill.fully_filled
    assert fill.costs_breakdown["turnover"] == pytest.approx(10 * 100.1)


# ---- no usable book: the order waits ----


@pytest.mark.parametrize("book", [None, "stale", "no_asks"])
async def test_without_a_usable_book_nothing_fills_and_the_signal_stays_pending(
    db_session_factory, redis, book
):
    subscription = await _subscription(db_session_factory)
    signal_id = await _signal(db_session_factory, subscription, "BUY")
    if book == "stale":
        await publish_depth(redis, SYMBOL, _book(age=120), ttl_seconds=60)
    elif book == "no_asks":
        await publish_depth(redis, SYMBOL, _book(asks=()), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill is None
    assert await _fills(db_session_factory, subscription) == []
    assert (await _position(db_session_factory, subscription)).quantity == 0
    assert not await _consumed(db_session_factory, signal_id)


async def test_the_waiting_order_fills_on_the_next_tick_once_a_fresh_book_arrives(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    signal_id = await _signal(db_session_factory, subscription, "BUY")
    assert await _tick(db_session_factory, redis, subscription) is None  # no book yet

    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)
    fill = await _tick(db_session_factory, redis, subscription)

    assert fill is not None and fill.filled_quantity == 49
    assert await _consumed(db_session_factory, signal_id)


async def test_a_stop_loss_cannot_exit_without_a_book_and_exits_when_there_is_one(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)  # long 49 @ ~100.14

    await redis.delete(depth_key(SYMBOL))
    assert await _tick(db_session_factory, redis, subscription, price=90.0) is None
    assert (await _position(db_session_factory, subscription)).quantity == 49  # still held

    await publish_depth(redis, SYMBOL, _book(bids=((90.0, 500),)), ttl_seconds=60)
    exit_fill = await _tick(db_session_factory, redis, subscription, price=90.0)

    assert exit_fill.side == "sell" and exit_fill.filled_quantity == 49
    assert exit_fill.avg_fill_price == 90.0
    assert (await _position(db_session_factory, subscription)).quantity == 0


async def test_a_pending_sell_signal_is_not_consumed_by_a_stop_loss_exit(db_session_factory, redis):
    """Only the order a signal asked for consumes it."""
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)
    sell_id = await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, SYMBOL, _book(bids=((90.0, 500),)), ttl_seconds=60)

    exit_fill = await _tick(db_session_factory, redis, subscription, price=90.0)  # stop-loss

    assert exit_fill.side == "sell" and exit_fill.filled_quantity == 49
    assert not await _consumed(db_session_factory, sell_id)


# ---- costs ----


async def test_a_delivery_buy_is_charged_stt_stamp_duty_exchange_fee_sebi_fee_and_gst(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    turnover = fill.filled_quantity * fill.avg_fill_price
    exchange = turnover * 0.0000297
    breakdown = fill.costs_breakdown
    assert breakdown["product"] == "cnc"
    assert breakdown["turnover"] == pytest.approx(turnover)
    assert breakdown["brokerage"] == 0.0  # free for delivery
    assert breakdown["stt"] == pytest.approx(turnover * 0.001)
    assert breakdown["stamp_duty"] == pytest.approx(turnover * 0.00015)
    assert breakdown["exchange_txn_charges"] == pytest.approx(exchange)
    assert breakdown["sebi_fee"] == pytest.approx(turnover * 0.000001)
    assert breakdown["gst"] == pytest.approx(0.18 * exchange)
    assert fill.costs_total == pytest.approx(
        sum(
            breakdown[k]
            for k in ("brokerage", "stt", "stamp_duty", "exchange_txn_charges", "sebi_fee", "gst")
        )
    )


async def test_a_delivery_sell_is_charged_stt_but_no_stamp_duty(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)
    await publish_depth(redis, SYMBOL, _book(bids=((90.0, 500),)), ttl_seconds=60)

    sell = await _tick(db_session_factory, redis, subscription, price=90.0)  # stop-loss

    assert sell.side == "sell"
    assert sell.costs_breakdown["stt"] == pytest.approx(sell.filled_quantity * 90.0 * 0.001)
    assert sell.costs_breakdown["stamp_duty"] == 0.0


async def test_the_position_keeps_gross_pnl_and_the_running_costs_apart(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)
    buy = await _tick(db_session_factory, redis, subscription)
    await publish_depth(redis, SYMBOL, _book(bids=((90.0, 500),)), ttl_seconds=60)
    sell = await _tick(db_session_factory, redis, subscription, price=90.0)

    position = await _position(db_session_factory, subscription)

    gross = (90.0 - buy.avg_fill_price) * 49
    assert position.realized_pnl == pytest.approx(gross)  # price difference only
    assert sell.realized_pnl == pytest.approx(gross)
    assert position.costs_total == pytest.approx(buy.costs_total + sell.costs_total)
    assert position.costs_total > 0


async def test_an_intraday_product_pays_brokerage_and_no_delivery_stt(
    db_session_factory, redis, monkeypatch
):
    monkeypatch.setenv("PAPER_TRADING_PRODUCT", "mis")
    get_settings.cache_clear()
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, SYMBOL, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    turnover = fill.filled_quantity * fill.avg_fill_price
    assert fill.costs_breakdown["product"] == "mis"
    assert fill.costs_breakdown["brokerage"] == pytest.approx(min(turnover * 0.0003, 20.0))
    assert fill.costs_breakdown["stt"] == 0.0  # intraday STT is on the sell leg only
    assert fill.costs_breakdown["stamp_duty"] == pytest.approx(turnover * 0.00003)


async def test_a_fill_that_filled_nothing_costs_nothing(db_session_factory, redis):
    from src.engine.paper_trading.order_book import FillResult
    from src.orchestration.paper_trading import fill_costs

    nothing = FillResult(10, 0, 10, None, False)

    assert fill_costs("buy", nothing) == (0.0, None)
