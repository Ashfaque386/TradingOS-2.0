"""APScheduler wiring tests for Build Spec §14's pipelines: symbols are
derived from real active subscriptions (never a fabricated universe), an
empty symbol set no-ops every job cleanly, the intraday job is
market-hours gated the same way Phase 7/9's tick jobs are, and the daily
ingestion job genuinely skips an NSE holiday end-to-end through the
scheduler's own entry point (not just the orchestration function it
calls).
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data import lake
from src.data.providers import FakeMarketDataProvider
from src.models.dataset_freshness_record import DatasetFreshnessRecord
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy import Strategy
from src.models.strategy_version import StrategyVersion
from src.orchestration import market_data_scheduler as scheduler_module


async def _make_strategy_and_version(db_session_factory) -> tuple:
    async with db_session_factory() as db:
        strategy = Strategy(name="S", objective="o")
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id, version_number=1, code="x=1", static_validation_passed=True
        )
        db.add(version)
        await db.commit()
        await db.refresh(strategy)
        await db.refresh(version)
        return strategy, version


async def _enroll_paper_subscription(db_session_factory, version, symbol: str) -> None:
    async with db_session_factory() as db:
        db.add(PaperTradingSubscription(strategy_version_id=version.id, symbol=symbol))
        await db.commit()


async def _enroll_live_subscription(db_session_factory, strategy, symbol: str) -> None:
    async with db_session_factory() as db:
        db.add(
            LiveTradingSubscription(strategy_id=strategy.id, symbol=symbol, broker_name="zerodha")
        )
        await db.commit()


async def test_active_symbols_is_empty_with_no_subscriptions(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    symbols = await scheduler_module._active_symbols(db_session_factory)
    assert symbols == []


async def test_active_symbols_unions_paper_and_live_subscriptions(
    db_session_factory: async_sessionmaker[AsyncSession],
):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    await _enroll_paper_subscription(db_session_factory, version, "PAPERSTOCK")
    await _enroll_live_subscription(db_session_factory, strategy, "LIVESTOCK")

    symbols = await scheduler_module._active_symbols(db_session_factory)
    assert symbols == ["LIVESTOCK", "PAPERSTOCK"]


async def test_daily_job_no_ops_with_no_active_symbols(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
):
    provider = FakeMarketDataProvider()
    await scheduler_module.run_incremental_daily_job(
        db_session_factory, provider, tmp_path / "lake"
    )
    assert lake.all_parquet_files(tmp_path / "lake") == []


async def test_daily_job_ingests_for_active_subscription_symbols(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    await _enroll_paper_subscription(db_session_factory, version, "DEMOSTOCK")
    provider = FakeMarketDataProvider()
    root = tmp_path / "lake"

    await scheduler_module.run_incremental_daily_job(db_session_factory, provider, root)

    async with db_session_factory() as db:
        result = await db.execute(
            select(DatasetFreshnessRecord).where(DatasetFreshnessRecord.symbol == "DEMOSTOCK")
        )
        rows = result.scalars().all()
    assert len(rows) == 1


async def test_intraday_job_skips_when_market_closed(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: False)
    strategy, version = await _make_strategy_and_version(db_session_factory)
    await _enroll_paper_subscription(db_session_factory, version, "DEMOSTOCK")
    root = tmp_path / "lake"

    await scheduler_module.run_intraday_ingestion_job(
        db_session_factory, FakeMarketDataProvider(), root
    )

    assert lake.all_parquet_files(root) == []


async def test_intraday_job_ingests_when_market_open(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(scheduler_module, "is_market_open_ist", lambda: True)
    strategy, version = await _make_strategy_and_version(db_session_factory)
    await _enroll_paper_subscription(db_session_factory, version, "DEMOSTOCK")
    root = tmp_path / "lake"

    await scheduler_module.run_intraday_ingestion_job(
        db_session_factory, FakeMarketDataProvider(), root
    )

    assert len(lake.all_parquet_files(root)) == 1


async def test_daily_job_skips_nse_holiday_via_real_calendar(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path, monkeypatch
):
    strategy, version = await _make_strategy_and_version(db_session_factory)
    await _enroll_paper_subscription(db_session_factory, version, "DEMOSTOCK")
    root = tmp_path / "lake"

    # Freeze "today" (as the job computes it) to a real fixed-date NSE
    # holiday (Republic Day) so this exercises the scheduler's own IST
    # "as_of = now" computation, not just the orchestration function with
    # an explicit date passed in.
    class _FrozenDatetime:
        @staticmethod
        def now(tz=None):
            import datetime as dt

            return dt.datetime(2026, 1, 26, 10, 0, tzinfo=tz)

    monkeypatch.setattr(scheduler_module, "datetime", _FrozenDatetime)

    await scheduler_module.run_incremental_daily_job(
        db_session_factory, FakeMarketDataProvider(), root
    )

    assert lake.all_parquet_files(root) == []
