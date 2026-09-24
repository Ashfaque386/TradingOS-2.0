"""Scheduler for the Investor Reporting Agent (Phase 19) -- weekly by
default (configurable via `cadence`), mirroring every other phase's
APScheduler shape. Fires Monday 07:00 IST, covering the prior Monday-Sunday
week, so a report for a completed week is ready before market open.
"""

from datetime import date, datetime, timedelta

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST
from src.observability.correlation import with_job_correlation_id
from src.orchestration.investor_reporting import generate_investor_report

logger = structlog.get_logger(__name__)

INVESTOR_REPORT_HOUR_IST = 7
INVESTOR_REPORT_MINUTE_IST = 0
INVESTOR_REPORT_JOB_ID = "investor_report_weekly_run"


def _prior_week(today: date) -> tuple[date, date]:
    # today is a Monday (the job's own cron day); the prior full week ran
    # Monday..Sunday just before it.
    period_end = today - timedelta(days=1)
    period_start = period_end - timedelta(days=6)
    return period_start, period_end


async def run_investor_report_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    period_start, period_end = _prior_week(datetime.now(IST).date())
    async with session_factory() as db:
        report = await generate_investor_report(
            db, period_start=period_start, period_end=period_end, cadence="weekly"
        )
    logger.info("investor_reporting.weekly_run_complete", report_id=str(report.id))


def start_investor_reporting_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(INVESTOR_REPORT_JOB_ID, run_investor_report_job),
        CronTrigger(
            day_of_week="mon",
            hour=INVESTOR_REPORT_HOUR_IST,
            minute=INVESTOR_REPORT_MINUTE_IST,
            timezone=IST,
        ),
        args=[session_factory],
        id=INVESTOR_REPORT_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
