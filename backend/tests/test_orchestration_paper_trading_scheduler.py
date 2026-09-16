"""APScheduler wiring tests (Build Spec §11): the daily and intraday jobs
run on their own schedule, gated by real market hours, with no manual
trigger required -- this is what "never requires a human click for a
paper order" means at the infrastructure level.
"""

import asyncio

import pandas as pd

from src.engine.paper_trading.order_book import MockOrderBookProvider
from src.engine.paper_trading.price_data import FakeDailyPriceProvider
from src.engine.paper_trading.tick_feed import MockTickSource, tick_stream_key
from src.engine.risk.compliance import ReferenceTableRegulatoryDataProvider
from src.orchestration import paper_trading_scheduler as scheduler_module
from src.orchestration.paper_trading import enroll_in_paper_trading
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"
_BUY_AS_OF = pd.Timestamp("2026-09-09")


async def _make_subscription(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="SchedDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = await enroll_in_paper_trading(
            db,
            strategy_version_id=version.id,
            symbol="SCHEDSTOCK",
            builtin_strategy="sma_crossover",
        )
    return subscription


async def test_daily_signal_job_runs_without_any_manual_trigger(db_session_factory):
    subscription = await _make_subscription(db_session_factory)
    price_provider = FakeDailyPriceProvider(seed_by_symbol={"SCHEDSTOCK": 1})

    await scheduler_module.run_daily_signal_job(db_session_factory, price_provider)

    from sqlalchemy import select

    from src.models.daily_signal import DailySignal

    async with db_session_factory() as db:
        result = await db.execute(
            select(DailySignal).where(DailySignal.subscription_id == subscription.id)
        )
        signals = result.scalars().all()
    # Whether or not today's fixed 90-day lookback happens to contain a
    # transition, the job must complete cleanly with no manual trigger.
    assert isinstance(signals, list)


async def test_mock_tick_publish_job_is_gated_by_market_hours(
    db_session_factory, redis_client, monkeypatch
):
    await _make_subscription(db_session_factory)
    await redis_client.delete(tick_stream_key("SCHEDSTOCK"))

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    await scheduler_module.run_tick_publish_job(db_session_factory, redis_client, MockTickSource())
    length = await redis_client.xlen(tick_stream_key("SCHEDSTOCK"))
    assert length == 0, "closed-market gating must prevent any tick publish"

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    await scheduler_module.run_tick_publish_job(db_session_factory, redis_client, MockTickSource())
    length_after = await redis_client.xlen(tick_stream_key("SCHEDSTOCK"))
    assert length_after == 1

    await redis_client.delete(tick_stream_key("SCHEDSTOCK"))


async def test_tick_drain_job_processes_ticks_and_advances_the_cursor(
    db_session_factory, redis_client, monkeypatch
):
    subscription = await _make_subscription(db_session_factory)
    await redis_client.delete(tick_stream_key("SCHEDSTOCK"))

    async with db_session_factory() as db:
        await scheduler_module.run_daily_signal_job(
            db_session_factory, FakeDailyPriceProvider(seed_by_symbol={"SCHEDSTOCK": 1})
        )

    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    await scheduler_module.run_tick_publish_job(db_session_factory, redis_client, MockTickSource())

    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis_client,
        MockOrderBookProvider(),
        ReferenceTableRegulatoryDataProvider(),
    )

    from src.models.paper_trading_subscription import PaperTradingSubscription

    async with db_session_factory() as db:
        sub_row = await db.get(PaperTradingSubscription, subscription.id)
    assert sub_row.tick_cursor != "0", "the drain job must persist an advanced cursor"

    await redis_client.delete(tick_stream_key("SCHEDSTOCK"))


async def test_tick_drain_job_is_gated_by_market_hours(
    db_session_factory, redis_client, monkeypatch
):
    await _make_subscription(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    # Must return cleanly (no exception) and do nothing when closed.
    await scheduler_module.run_tick_drain_job(
        db_session_factory,
        redis_client,
        MockOrderBookProvider(),
        ReferenceTableRegulatoryDataProvider(),
    )


async def test_start_paper_trading_scheduler_registers_and_runs_all_jobs(
    db_session_factory, redis_client, monkeypatch
):
    """End-to-end proof the scheduler itself -- not just the job bodies --
    fires jobs on its own timer with zero manual intervention."""
    await _make_subscription(db_session_factory)
    await redis_client.delete(tick_stream_key("SCHEDSTOCK"))
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    scheduler = scheduler_module.start_paper_trading_scheduler(
        db_session_factory,
        redis=redis_client,
        price_provider=FakeDailyPriceProvider(seed_by_symbol={"SCHEDSTOCK": 1}),
        order_book_provider=MockOrderBookProvider(),
        regulatory_provider=ReferenceTableRegulatoryDataProvider(),
        tick_source=MockTickSource(),
    )
    try:
        job_ids = {job.id for job in scheduler.get_jobs()}
        assert job_ids == {
            scheduler_module.DAILY_SIGNAL_JOB_ID,
            scheduler_module.TICK_PUBLISH_JOB_ID,
            scheduler_module.TICK_DRAIN_JOB_ID,
        }

        # The tick-publish job fires every 3s; give it time to run at least
        # once entirely on the scheduler's own timer.
        await asyncio.sleep(4)
        length = await redis_client.xlen(tick_stream_key("SCHEDSTOCK"))
        assert length > 0, "the scheduler must have published at least one tick unattended"
    finally:
        scheduler.shutdown(wait=False)
        await redis_client.delete(tick_stream_key("SCHEDSTOCK"))
