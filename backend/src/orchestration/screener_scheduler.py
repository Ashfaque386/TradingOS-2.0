"""Scheduler for the Screener Agent (Phase 19, Build Spec extension) --
mirrors every other phase's APScheduler shape (Phase 7/9/10/11/12). Runs
once a day, before the paper/live daily-signal jobs (both at 08:00 IST), so
a screener-created strategy already exists in time for the same day's
signal generation once it reaches Paper/Live Trading (a later, separate
human/autonomy gate, unchanged by this scheduler).
"""

import pandas as pd
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST
from src.engine.paper_trading.price_data import PriceDataProvider
from src.observability.correlation import with_job_correlation_id
from src.orchestration.screener import ScreenerFilters, run_screener_and_generate_strategies

logger = structlog.get_logger(__name__)

SCREENER_HOUR_IST = 7
SCREENER_MINUTE_IST = 30
SCREENER_JOB_ID = "screener_daily_run"


async def run_screener_job(
    session_factory: async_sessionmaker[AsyncSession],
    price_provider: PriceDataProvider,
    filters: ScreenerFilters,
) -> None:
    async with session_factory() as db:
        created = await run_screener_and_generate_strategies(
            db,
            price_provider,
            # tz-naive, matching every other daily-signal scheduler
            # (live_trading_scheduler.py, paper_trading_scheduler.py) --
            # both the real data lake's own index (src/data/lake.py) and
            # FakeDailyPriceProvider's fallback index are tz-naive, so a
            # tz-aware `as_of` here made every `.loc[:as_of]` slice raise
            # "Cannot compare tz-naive and tz-aware datetime-like objects."
            as_of=pd.Timestamp.now(tz=IST).normalize().tz_localize(None),
            filters=filters,
        )
    logger.info("screener.daily_run_complete", strategies_created=len(created))


def start_screener_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    price_provider: PriceDataProvider,
    filters: ScreenerFilters | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(SCREENER_JOB_ID, run_screener_job),
        CronTrigger(hour=SCREENER_HOUR_IST, minute=SCREENER_MINUTE_IST, timezone=IST),
        args=[session_factory, price_provider, filters or ScreenerFilters()],
        id=SCREENER_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
