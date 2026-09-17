"""Autonomous scheduling for every Build Spec §14 pipeline: APScheduler
drives ingestion without any manual trigger, mirroring Phase 7/9's
scheduler shape -- daily IST cron jobs plus a market-hours-gated interval
job. The explicit fix this phase makes over a prior build: incremental
daily ingestion, corporate actions ingestion, instrument master sync, and
intraday minute-bar ingestion are all wired into this scheduler from the
start, not left manual/dormant.

**Symbol universe**: there is no separate "watchlist" concept in this
codebase -- the set of symbols worth ingesting is exactly the symbols
currently in active use, i.e. every distinct `symbol` across active
`PaperTradingSubscription` and `LiveTradingSubscription` rows. A fresh
install with no subscriptions yet simply has nothing to ingest; every job
below no-ops (skips, no provenance row) rather than erroring on an empty
symbol list.

**Timing**: the daily reference-data jobs (incremental OHLCV, corporate
actions, instrument master) run in the evening, *after* NSE close
(15:30 IST) -- ingesting "today" only makes sense once today's session has
actually happened. Phase 7/9's existing 08:00 IST daily-signal jobs then
read that data the next morning, already ingested the evening before.
Catalog refresh and backup run later still, after the day's writes are
done.
"""

from datetime import datetime, timedelta
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.data.providers import MarketDataProvider
from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.models.live_trading_subscription import LiveTradingSubscription
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration import market_data as market_data_orch

logger = structlog.get_logger(__name__)

INCREMENTAL_DAILY_HOUR_IST = 18
CORPORATE_ACTIONS_HOUR_IST = 18
CORPORATE_ACTIONS_MINUTE_IST = 5
INSTRUMENT_MASTER_HOUR_IST = 18
INSTRUMENT_MASTER_MINUTE_IST = 10
CATALOG_REFRESH_HOUR_IST = 22
BACKUP_HOUR_IST = 22
BACKUP_MINUTE_IST = 15
INTRADAY_INGESTION_INTERVAL_SECONDS = 300
CORPORATE_ACTIONS_LOOKBACK_DAYS = 400

INCREMENTAL_DAILY_JOB_ID = "market_data_incremental_daily"
CORPORATE_ACTIONS_JOB_ID = "market_data_corporate_actions"
INSTRUMENT_MASTER_JOB_ID = "market_data_instrument_master"
INTRADAY_INGESTION_JOB_ID = "market_data_intraday_ingestion"
CATALOG_REFRESH_JOB_ID = "market_data_catalog_refresh"
BACKUP_JOB_ID = "market_data_backup"


async def _active_symbols(session_factory: async_sessionmaker[AsyncSession]) -> list[str]:
    async with session_factory() as db:
        paper = await db.execute(
            select(PaperTradingSubscription.symbol).where(
                PaperTradingSubscription.is_active.is_(True)
            )
        )
        live = await db.execute(
            select(LiveTradingSubscription.symbol).where(
                LiveTradingSubscription.is_active.is_(True)
            )
        )
        symbols = {row[0] for row in paper.all()} | {row[0] for row in live.all()}
    return sorted(symbols)


async def run_incremental_daily_job(
    session_factory: async_sessionmaker[AsyncSession], provider: MarketDataProvider, root: Path
) -> None:
    symbols = await _active_symbols(session_factory)
    if not symbols:
        return
    as_of = datetime.now(IST).date()
    async with session_factory() as db:
        result = await market_data_orch.run_incremental_daily_ingestion(
            db, provider=provider, root=root, symbols=symbols, as_of=as_of
        )
    logger.info("market_data.incremental_daily_job_ran", status=result.status, symbols=len(symbols))


async def run_corporate_actions_job(
    session_factory: async_sessionmaker[AsyncSession], provider: MarketDataProvider
) -> None:
    symbols = await _active_symbols(session_factory)
    if not symbols:
        return
    since = datetime.now(IST).date() - timedelta(days=CORPORATE_ACTIONS_LOOKBACK_DAYS)
    async with session_factory() as db:
        result = await market_data_orch.run_corporate_actions_ingestion(
            db, provider=provider, symbols=symbols, since=since
        )
    logger.info("market_data.corporate_actions_job_ran", status=result.status, symbols=len(symbols))


async def run_instrument_master_job(
    session_factory: async_sessionmaker[AsyncSession], provider: MarketDataProvider
) -> None:
    symbols = await _active_symbols(session_factory)
    if not symbols:
        return
    async with session_factory() as db:
        result = await market_data_orch.run_instrument_master_sync(
            db, provider=provider, symbols=symbols
        )
    logger.info("market_data.instrument_master_job_ran", status=result.status, symbols=len(symbols))


async def run_intraday_ingestion_job(
    session_factory: async_sessionmaker[AsyncSession], provider: MarketDataProvider, root: Path
) -> None:
    if not is_market_open_ist():
        return
    symbols = await _active_symbols(session_factory)
    if not symbols:
        return
    day = datetime.now(IST).date()
    async with session_factory() as db:
        result = await market_data_orch.run_intraday_ingestion(
            db, provider=provider, root=root, symbols=symbols, day=day
        )
    logger.info(
        "market_data.intraday_ingestion_job_ran", status=result.status, symbols=len(symbols)
    )


async def run_catalog_refresh_job(
    session_factory: async_sessionmaker[AsyncSession], root: Path
) -> None:
    async with session_factory() as db:
        result = await market_data_orch.run_catalog_refresh(db, root=root)
    logger.info(
        "market_data.catalog_refresh_job_ran",
        daily_rows=result.details.get("ohlcv_daily_rows") if result.details else None,
        intraday_rows=result.details.get("ohlcv_intraday_rows") if result.details else None,
    )


async def run_backup_job(
    session_factory: async_sessionmaker[AsyncSession], root: Path, backup_root: Path
) -> None:
    async with session_factory() as db:
        result = await market_data_orch.run_nightly_backup(db, root=root, backup_root=backup_root)
    if result.status != "success":
        logger.warning("market_data.backup_job_failed_validation", error=result.error_message)
    else:
        logger.info("market_data.backup_job_ran", files=result.symbols_processed)


def start_market_data_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    provider: MarketDataProvider,
    root: Path,
    backup_root: Path,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)

    scheduler.add_job(
        run_incremental_daily_job,
        CronTrigger(hour=INCREMENTAL_DAILY_HOUR_IST, minute=0, timezone=IST),
        args=[session_factory, provider, root],
        id=INCREMENTAL_DAILY_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_corporate_actions_job,
        CronTrigger(
            hour=CORPORATE_ACTIONS_HOUR_IST, minute=CORPORATE_ACTIONS_MINUTE_IST, timezone=IST
        ),
        args=[session_factory, provider],
        id=CORPORATE_ACTIONS_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_instrument_master_job,
        CronTrigger(
            hour=INSTRUMENT_MASTER_HOUR_IST, minute=INSTRUMENT_MASTER_MINUTE_IST, timezone=IST
        ),
        args=[session_factory, provider],
        id=INSTRUMENT_MASTER_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_intraday_ingestion_job,
        "interval",
        seconds=INTRADAY_INGESTION_INTERVAL_SECONDS,
        args=[session_factory, provider, root],
        id=INTRADAY_INGESTION_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_catalog_refresh_job,
        CronTrigger(hour=CATALOG_REFRESH_HOUR_IST, minute=0, timezone=IST),
        args=[session_factory, root],
        id=CATALOG_REFRESH_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        run_backup_job,
        CronTrigger(hour=BACKUP_HOUR_IST, minute=BACKUP_MINUTE_IST, timezone=IST),
        args=[session_factory, root, backup_root],
        id=BACKUP_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
