"""Scheduler for the Post-Trade Review Agent (Phase 19) -- runs once a day
after NSE close (15:30 IST), mirroring every other phase's APScheduler
shape (Phase 7/9/10/11/12). Runs unconditionally, same posture as the
audit and notification schedulers -- a day with no trades still produces
a real (empty) review, not a suppressed one.
"""

from datetime import datetime

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST
from src.observability.correlation import with_job_correlation_id
from src.orchestration.post_trade_review import run_post_trade_review

logger = structlog.get_logger(__name__)

POST_TRADE_REVIEW_HOUR_IST = 16
POST_TRADE_REVIEW_MINUTE_IST = 0
POST_TRADE_REVIEW_JOB_ID = "post_trade_review_daily_run"


async def run_post_trade_review_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        findings = await run_post_trade_review(db, as_of=datetime.now(IST).date())
    logger.info("post_trade_review.daily_run_complete", findings=len(findings))


def start_post_trade_review_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(POST_TRADE_REVIEW_JOB_ID, run_post_trade_review_job),
        CronTrigger(
            hour=POST_TRADE_REVIEW_HOUR_IST, minute=POST_TRADE_REVIEW_MINUTE_IST, timezone=IST
        ),
        args=[session_factory],
        id=POST_TRADE_REVIEW_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
