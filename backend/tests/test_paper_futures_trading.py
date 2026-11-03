"""A futures subscription trading its contract on real depth (phase 4).

The subscription reads signals from the underlying (NIFTY) and trades the monthly contract
(`NFO:NIFTY26OCTFUT`): its tick stream, book, fills and position are the contract's. Entries are
whole lots, long or short; an entry must fit the account's margin; exits never need margin; and a
future is charged the futures cost schedule.
"""

import time
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import select

from src.brokers.base import FuturesContract
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
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.paper_trading import enroll_in_paper_trading, process_tick
from src.orchestration.strategies import create_strategy, create_version_with_validation

CONTRACT = FuturesContract("NFO:NIFTY26OCTFUT", "NIFTY", date(2026, 10, 27), 65, 10.0)
_CODE = "def run_backtest(data, config):\n    return {}\n"
_REGULATORY = ReferenceTableRegulatoryDataProvider()


def _book(*, bid=23_995.0, ask=24_005.0, quantity=1_000):
    return DepthSnapshot(
        bids=(OrderBookLevel(bid, quantity), OrderBookLevel(bid - 5, quantity)),
        asks=(OrderBookLevel(ask, quantity), OrderBookLevel(ask + 5, quantity)),
        timestamp_ms=int(time.time() * 1000),
    )


@pytest_asyncio.fixture
async def redis(redis_client):
    await redis_client.delete(depth_key(CONTRACT.symbol))
    yield redis_client
    await redis_client.delete(depth_key(CONTRACT.symbol))


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.delenv("PAPER_FRICTION_OVERRIDES", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _subscription(db_session_factory, *, lots=1, capital=1_000_000.0, contract=CONTRACT):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name=f"FUT-{uuid.uuid4().hex[:6]}", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        return await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="NIFTY",
            initial_capital=capital,
            stop_loss_pct=3.0,
            contract=contract,
            lots=lots,
        )


async def _signal(db_session_factory, subscription, kind: str):
    async with db_session_factory() as db:
        db.add(
            DailySignal(
                subscription_id=subscription.id,
                symbol="NIFTY",
                signal_type=kind,
                reference_price=24_000.0,
            )
        )
        await db.commit()


async def _tick(db_session_factory, redis, subscription, price=24_000.0):
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


# ---- what a futures subscription is ----


async def test_enrolling_with_a_contract_makes_a_futures_subscription(db_session_factory):
    subscription = await _subscription(db_session_factory, lots=3)

    async with db_session_factory() as db:
        stored = await db.get(PaperTradingSubscription, subscription.id)
    assert stored.instrument_kind == "future"
    assert (stored.symbol, stored.contract_symbol) == ("NIFTY", "NFO:NIFTY26OCTFUT")
    assert (stored.contract_expiry, stored.lot_size, stored.lots) == (date(2026, 10, 27), 65, 3)
    assert stored.trading_symbol == "NFO:NIFTY26OCTFUT"


async def test_enrolling_without_a_contract_is_an_equity_subscription_as_before(
    db_session_factory,
):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="EQ", objective="o")
        version = await create_version_with_validation(db, strategy, _CODE)
        stored = await enroll_in_paper_trading(db, strategy_version_id=version.id, symbol="INFY")

    assert stored.instrument_kind == "equity"
    assert (stored.contract_symbol, stored.lot_size, stored.lots) == (None, None, 1)


# ---- entries ----


async def test_a_buy_signal_buys_whole_lots_of_the_contract_on_its_own_book(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory, lots=2)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill.symbol == "NFO:NIFTY26OCTFUT"  # the contract, not the underlying
    assert fill.side == "buy" and fill.filled_quantity == 130  # 2 lots of 65
    assert fill.avg_fill_price == 24_005.0 and fill.data_source == "real"
    position = await _position(db_session_factory, subscription)
    assert (position.symbol, position.quantity) == ("NFO:NIFTY26OCTFUT", 130)


async def test_a_sell_signal_with_no_position_opens_a_short(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill.side == "sell" and fill.filled_quantity == 65
    assert fill.avg_fill_price == 23_995.0  # sold into the bid
    position = await _position(db_session_factory, subscription)
    assert position.quantity == -65 and position.avg_cost == 23_995.0


async def test_the_entry_is_not_held_to_the_cash_equity_notional_limit(db_session_factory, redis):
    """One lot is Rs 15.6 lakh of notional against Rs 6 lakh of capital -- far past the 10%
    position limit that suits shares. Judged as margin it fits, so the entry happens."""
    subscription = await _subscription(db_session_factory, capital=600_000.0)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill is not None and fill.filled_quantity == 65


async def test_an_entry_whose_margin_does_not_fit_the_account_is_refused(db_session_factory, redis):
    subscription = await _subscription(db_session_factory, capital=100_000.0)  # needs ~Rs 2.3 lakh
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill is None
    assert await _fills(db_session_factory, subscription) == []
    assert (await _position(db_session_factory, subscription)).quantity == 0


async def test_the_margin_limit_follows_the_settings(db_session_factory, redis, monkeypatch):
    subscription = await _subscription(db_session_factory, capital=1_000_000.0)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)
    monkeypatch.setenv("PAPER_FUTURES_MAX_MARGIN_UTILISATION_PCT", "20")  # Rs 2 lakh < Rs 2.34 lakh
    get_settings.cache_clear()

    assert await _tick(db_session_factory, redis, subscription) is None

    monkeypatch.setenv("PAPER_FUTURES_MARGIN_PCT", "10")  # now it needs Rs 1.56 lakh
    get_settings.cache_clear()
    assert await _tick(db_session_factory, redis, subscription) is not None


# ---- exits ----


async def test_a_buy_signal_covers_a_short_at_a_gain_when_the_price_fell(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)  # short 65 @ 23,995
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(bid=23_895.0, ask=23_905.0), ttl_seconds=60)

    cover = await _tick(db_session_factory, redis, subscription, price=23_900.0)

    assert cover.side == "buy" and cover.filled_quantity == 65
    assert cover.avg_fill_price == 23_905.0
    assert cover.realized_pnl == pytest.approx((23_995.0 - 23_905.0) * 65)  # gross, in rupees
    assert (await _position(db_session_factory, subscription)).quantity == 0


async def test_a_short_is_stopped_out_when_the_price_rises_through_the_stop(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)  # short @ 23,995
    await publish_depth(redis, CONTRACT.symbol, _book(bid=24_795.0, ask=24_805.0), ttl_seconds=60)

    stop = await _tick(db_session_factory, redis, subscription, price=24_800.0)  # +3.35%

    assert stop.side == "buy" and stop.filled_quantity == 65
    assert stop.realized_pnl < 0
    assert (await _position(db_session_factory, subscription)).quantity == 0


async def test_an_exit_needs_no_margin_even_when_the_account_could_no_longer_enter(
    db_session_factory, redis
):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)
    async with db_session_factory() as db:
        stored = await db.get(PaperTradingSubscription, subscription.id)
        stored.initial_capital = 10_000.0  # far too little to open a lot now
        await db.commit()
    await _signal(db_session_factory, subscription, "BUY")

    cover = await _tick(db_session_factory, redis, subscription, price=24_000.0)

    assert cover is not None and cover.side == "buy"


# ---- costs ----


async def test_a_futures_buy_pays_stamp_duty_and_no_stt(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    turnover = 65 * 24_005.0
    costs = fill.costs_breakdown
    assert (costs["segment"], costs["product"]) == ("future", "nrml")
    assert costs["turnover"] == pytest.approx(turnover)
    assert costs["brokerage"] == 20.0  # the Rs 20 cap: 0.03% would be ~Rs 468
    assert costs["stt"] == 0.0
    assert costs["stamp_duty"] == pytest.approx(turnover * 0.00002)
    assert costs["exchange_txn_charges"] == pytest.approx(
        turnover * (0.0000183 + 0.000000001)
    )  # + IPFT
    assert costs["gst"] == pytest.approx(
        0.18 * (costs["brokerage"] + costs["sebi_fee"] + costs["exchange_txn_charges"])
    )


async def test_a_futures_sell_pays_stt_and_no_stamp_duty(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)

    fill = await _tick(db_session_factory, redis, subscription)

    assert fill.costs_breakdown["stt"] == pytest.approx(65 * 23_995.0 * 0.0005)
    assert fill.costs_breakdown["stamp_duty"] == 0.0


async def test_the_position_accumulates_the_costs_of_both_legs(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "SELL")
    await publish_depth(redis, CONTRACT.symbol, _book(), ttl_seconds=60)
    await _tick(db_session_factory, redis, subscription)
    await _signal(db_session_factory, subscription, "BUY")
    await _tick(db_session_factory, redis, subscription)

    fills = await _fills(db_session_factory, subscription)
    position = await _position(db_session_factory, subscription)

    assert [f.side for f in fills] == ["sell", "buy"]
    assert position.costs_total == pytest.approx(sum(f.costs_total for f in fills))
    assert position.costs_total > 0


# ---- the book is the contract's ----


async def test_the_underlyings_book_is_not_used_for_its_contract(db_session_factory, redis):
    subscription = await _subscription(db_session_factory)
    await _signal(db_session_factory, subscription, "BUY")
    await publish_depth(
        redis, "NIFTY", _book(), ttl_seconds=60
    )  # the index has a book, not the future
    try:
        fill = await _tick(db_session_factory, redis, subscription)
    finally:
        await redis.delete(depth_key("NIFTY"))

    assert fill is None  # no book for the contract: the order waits
