"""The paper engine judges an order against the exchange's own circuit limits when the tick carried
them, and does not hold a stop-loss exit to the circuit band (a gap past the band is when the stop
matters). The drain job hands each tick's limits to the engine.
"""

import time

import pytest_asyncio

from src.engine.paper_trading.tick_feed import Tick, publish_tick, tick_stream_key
from src.engine.risk.compliance import ExchangeCircuitLimits
from src.models.paper_fill import PaperFill
from src.orchestration import paper_trading_scheduler as scheduler
from src.orchestration.paper_trading import process_tick, run_daily_signal_generation
from tests.test_engine_alert_events_wiring import (
    _BOOKS,
    _BUY_DAY,
    _PRICES,
    _REGULATORY,
    _subscription,
)


async def _signal_price(db_session_factory) -> float:
    async with db_session_factory() as db:
        [signal] = await run_daily_signal_generation(db, price_provider=_PRICES, as_of=_BUY_DAY)
    return signal.reference_price


async def _tick(db_session_factory, subscription, price, *, limits=None):
    async with db_session_factory() as db:
        return await process_tick(
            db,
            subscription_id=subscription.id,
            tick_price=price,
            order_book_provider=_BOOKS,
            regulatory_provider=_REGULATORY,
            exchange_limits=limits,
        )


async def test_an_entry_at_the_exchanges_upper_limit_is_refused_and_tried_again_below_it(
    db_session_factory,
):
    subscription = await _subscription(db_session_factory)
    price = await _signal_price(db_session_factory)
    at_the_limit = ExchangeCircuitLimits(lower=price * 0.9, upper=price)

    assert await _tick(db_session_factory, subscription, price, limits=at_the_limit) is None

    roomier = ExchangeCircuitLimits(lower=price * 0.9, upper=price * 1.1)
    fill = await _tick(db_session_factory, subscription, price, limits=roomier)
    assert fill is not None and fill.side == "buy"  # the signal was not used up by the refusal


async def test_an_entry_with_no_limits_falls_back_to_the_reference_tables_band(db_session_factory):
    subscription = await _subscription(db_session_factory)
    price = await _signal_price(db_session_factory)

    fill = await _tick(db_session_factory, subscription, price)

    assert fill is not None and fill.side == "buy"


async def _in_a_position(db_session_factory):
    subscription = await _subscription(db_session_factory)
    price = await _signal_price(db_session_factory)
    entry = await _tick(db_session_factory, subscription, price)
    return subscription, entry.avg_fill_price


async def test_a_stop_loss_exit_after_a_gap_beyond_the_band_still_happens(db_session_factory):
    subscription, cost = await _in_a_position(db_session_factory)

    # 30% under the cost: past the 20% reference-table band, which used to refuse the exit.
    exit_fill = await _tick(db_session_factory, subscription, cost * 0.7)

    assert exit_fill is not None
    assert (exit_fill.side, exit_fill.realized_pnl < 0) == ("sell", True)


async def test_a_stop_loss_exit_at_the_exchanges_lower_limit_still_happens(db_session_factory):
    subscription, cost = await _in_a_position(db_session_factory)
    limits = ExchangeCircuitLimits(lower=cost * 0.8, upper=cost * 1.2)

    exit_fill = await _tick(db_session_factory, subscription, cost * 0.75, limits=limits)

    assert exit_fill is not None and exit_fill.side == "sell"


# ---- the drain job hands each tick's limits to the engine ----


@pytest_asyncio.fixture
async def redis(redis_client):
    await redis_client.delete(tick_stream_key("DEMOSTOCK"))
    yield redis_client
    await redis_client.delete(tick_stream_key("DEMOSTOCK"))


async def _drain_with(db_session_factory, redis, monkeypatch, tick):
    seen = []

    async def recorder(db, **kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(scheduler, "process_tick", recorder)
    monkeypatch.setattr(scheduler, "is_market_open_ist", lambda *a, **k: True)
    await _subscription(db_session_factory)
    await publish_tick(redis, tick)
    await scheduler.run_tick_drain_job(db_session_factory, redis, _BOOKS, _REGULATORY)
    return seen


async def test_the_drain_job_passes_the_ticks_circuit_limits_on(
    db_session_factory, redis, monkeypatch
):
    tick = Tick(
        "DEMOSTOCK",
        100.0,
        int(time.time() * 1000),
        "real",
        lower_circuit=90.0,
        upper_circuit=110.0,
    )

    [call] = await _drain_with(db_session_factory, redis, monkeypatch, tick)

    assert call["exchange_limits"] == ExchangeCircuitLimits(lower=90.0, upper=110.0)
    assert call["tick_price"] == 100.0


async def test_a_tick_with_no_limits_passes_none(db_session_factory, redis, monkeypatch):
    tick = Tick("DEMOSTOCK", 100.0, int(time.time() * 1000), "real")

    [call] = await _drain_with(db_session_factory, redis, monkeypatch, tick)

    assert call["exchange_limits"] is None


async def test_no_fills_are_left_behind_by_the_refused_entry(db_session_factory):
    subscription = await _subscription(db_session_factory)
    price = await _signal_price(db_session_factory)

    await _tick(
        db_session_factory,
        subscription,
        price,
        limits=ExchangeCircuitLimits(lower=price * 0.9, upper=price),
    )

    from sqlalchemy import select

    async with db_session_factory() as db:
        assert (await db.execute(select(PaperFill))).scalars().all() == []
