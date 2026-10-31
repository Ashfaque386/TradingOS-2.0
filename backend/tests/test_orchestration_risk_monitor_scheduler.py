"""The drawdown monitor's schedule (Phase 3): a reading each minute while the market is open and
one at the close, a failure in one mode never stopping the other, and nothing read when the market
is shut.
"""

from sqlalchemy import select

from src.models.equity_snapshot import EquitySnapshot
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration import risk_monitor_scheduler as scheduler_module
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def _capital(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="MonitorSched", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        db.add(
            PaperTradingSubscription(
                strategy_version_id=version.id,
                symbol="MONSCHED",
                builtin_strategy="always_long",
                initial_capital=100_000.0,
            )
        )
        await db.commit()


async def _rows(db_session_factory):
    async with db_session_factory() as db:
        return list((await db.execute(select(EquitySnapshot))).scalars().all())


async def test_the_scheduler_has_exactly_the_reading_and_the_close_job(
    db_session_factory, redis_client
):
    scheduler = scheduler_module.start_risk_monitor_scheduler(
        db_session_factory, redis=redis_client
    )
    try:
        assert {job.id for job in scheduler.get_jobs()} == {
            scheduler_module.EQUITY_READING_JOB_ID,
            scheduler_module.END_OF_DAY_JOB_ID,
        }
        reading = scheduler.get_job(scheduler_module.EQUITY_READING_JOB_ID)
        assert reading.trigger.interval.total_seconds() == 60
        close = str(scheduler.get_job(scheduler_module.END_OF_DAY_JOB_ID).trigger)
        assert "hour='15'" in close and "minute='45'" in close
        assert "mon-fri" in close
    finally:
        scheduler.shutdown(wait=False)


async def test_while_the_market_is_open_both_modes_are_read(
    db_session_factory, redis_client, monkeypatch
):
    await _capital(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)

    await scheduler_module.run_equity_reading_job(db_session_factory, redis_client)

    rows = await _rows(db_session_factory)
    assert sorted(r.mode for r in rows) == ["live", "paper"]
    paper = next(r for r in rows if r.mode == "paper")
    assert paper.equity == 100_000.0
    live = next(r for r in rows if r.mode == "live")
    assert live.equity is None  # no LIVE_STARTING_CAPITAL: a gap, stored as one


async def test_while_the_market_is_shut_the_interval_job_reads_nothing(
    db_session_factory, redis_client, monkeypatch
):
    await _capital(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)

    await scheduler_module.run_equity_reading_job(db_session_factory, redis_client)

    assert await _rows(db_session_factory) == []


async def test_the_close_reads_whether_or_not_the_market_is_open(
    db_session_factory, redis_client, monkeypatch
):
    await _capital(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)

    await scheduler_module.run_end_of_day_reading_job(db_session_factory, redis_client)

    assert sorted(r.mode for r in await _rows(db_session_factory)) == ["live", "paper"]


async def test_one_mode_failing_does_not_stop_the_other_or_the_job(
    db_session_factory, redis_client, monkeypatch
):
    await _capital(db_session_factory)
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    real = scheduler_module.measure_equity

    async def flaky(db, redis, mode):
        if mode == "paper":
            raise RuntimeError("redis went away")
        return await real(db, redis, mode)

    monkeypatch.setattr(scheduler_module, "measure_equity", flaky)

    await scheduler_module.run_equity_reading_job(db_session_factory, redis_client)

    assert [r.mode for r in await _rows(db_session_factory)] == ["live"]
