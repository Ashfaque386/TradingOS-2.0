"""Autonomous scheduling for the drawdown monitor (Phase 3): measure each mode's equity, store the
reading and trip the kill switch when the drawdown reaches the limit, with nobody asking.

Two jobs, like the other schedulers:

* a **minute-by-minute reading while the market is open** -- the only time prices move and a
  drawdown can grow, so outside it the job returns at once (a fast no-op, not a cancelled job);
* an **end-of-day reading at 15:45 IST** on trading days regardless, so the chart always has the
  day's close and a position that ended the day unpriced shows up as a gap, not as nothing.

A failure measuring or evaluating one mode never stops the other, and never stops the job: the next
firing tries again.
"""

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST, is_market_open_ist
from src.engine.risk.kill_switch import KillSwitchMode
from src.observability.correlation import with_job_correlation_id
from src.orchestration.drawdown_monitor import evaluate_mode_drawdown
from src.orchestration.equity import measure_equity

logger = structlog.get_logger(__name__)

MODES: tuple[KillSwitchMode, ...] = ("paper", "live")

EQUITY_READING_INTERVAL_SECONDS = 60
END_OF_DAY_HOUR_IST = 15
END_OF_DAY_MINUTE_IST = 45

EQUITY_READING_JOB_ID = "risk_monitor_equity_reading"
END_OF_DAY_JOB_ID = "risk_monitor_end_of_day"


async def run_equity_reading_job(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis, *, only_when_open: bool = True
) -> None:
    if only_when_open and not is_market_open_ist():
        return
    for mode in MODES:
        try:
            async with session_factory() as db:
                sample = await measure_equity(db, redis, mode)
                outcome = await evaluate_mode_drawdown(db, sample)
        except Exception:  # noqa: BLE001 - one mode failing must not stop the other, or the job
            logger.exception("risk_monitor.reading_failed", mode=mode)
            continue
        if outcome.status == "tripped":
            logger.error(
                "risk_monitor.kill_switch_tripped",
                mode=mode,
                drawdown_pct=outcome.drawdown_pct,
                threshold_pct=outcome.threshold_pct,
            )
        elif outcome.status == "cannot_evaluate":
            logger.info("risk_monitor.cannot_evaluate", mode=mode, note=sample.note)


async def run_end_of_day_reading_job(
    session_factory: async_sessionmaker[AsyncSession], redis: Redis
) -> None:
    await run_equity_reading_job(session_factory, redis, only_when_open=False)


def start_risk_monitor_scheduler(
    session_factory: async_sessionmaker[AsyncSession], *, redis: Redis
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        with_job_correlation_id(EQUITY_READING_JOB_ID, run_equity_reading_job),
        IntervalTrigger(seconds=EQUITY_READING_INTERVAL_SECONDS),
        args=[session_factory, redis],
        id=EQUITY_READING_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(END_OF_DAY_JOB_ID, run_end_of_day_reading_job),
        CronTrigger(
            day_of_week="mon-fri",
            hour=END_OF_DAY_HOUR_IST,
            minute=END_OF_DAY_MINUTE_IST,
            timezone=IST,
        ),
        args=[session_factory, redis],
        id=END_OF_DAY_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
