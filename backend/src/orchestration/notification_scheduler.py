"""Autonomous scheduling for the fourth fixed alert level (Build Spec §18,
app/settings/page.tsx's ALERT_LEVELS: "daily" summary) -- the other three
(kill-switch, sign-off, go-live) fire synchronously from the real event
that produces them (src.orchestration.kill_switch/approvals/strategies);
a daily summary has no single triggering event, so it's the one alert
level that genuinely needs a scheduled job of its own, mirroring every
other phase's APScheduler shape (Phase 7/9/10/11).

Runs once a day, unconditionally (not `is_market_open_ist()`-gated, same
posture as Phase 11's audit scheduler) -- a summary of "nothing happened
today" on a holiday is still a real, honest status, not something to
suppress.
"""

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.engine.paper_trading.market_hours import IST
from src.models.approval_request import ApprovalRequest, ApprovalStatus
from src.models.kill_switch_state import KillSwitchState
from src.models.live_order_intent import LiveOrderIntent
from src.notifications.dispatch import notify
from src.notifications.types import AlertLevel
from src.observability.correlation import with_job_correlation_id

logger = structlog.get_logger(__name__)

DAILY_SUMMARY_HOUR_IST = 18
DAILY_SUMMARY_MINUTE_IST = 0
DAILY_SUMMARY_JOB_ID = "notifications_daily_summary"


async def run_daily_summary_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as db:
        kill_switch_rows = (await db.execute(select(KillSwitchState))).scalars().all()
        tripped_modes = [row.mode for row in kill_switch_rows if row.tripped]

        pending_approvals = (
            await db.execute(
                select(func.count())
                .select_from(ApprovalRequest)
                .where(ApprovalRequest.status == ApprovalStatus.PENDING)
            )
        ).scalar_one()

        pending_intents = (
            await db.execute(
                select(func.count())
                .select_from(LiveOrderIntent)
                .where(LiveOrderIntent.status == "pending_approval")
            )
        ).scalar_one()

    kill_switch_status = (
        "TRIPPED (" + ", ".join(tripped_modes) + ")" if tripped_modes else "armed, not tripped"
    )
    body = (
        f"Kill switch: {kill_switch_status}. "
        f"Pending sign-offs: {pending_approvals} strategy approval(s), "
        f"{pending_intents} live order intent(s)."
    )
    await notify(
        AlertLevel.DAILY,
        title="TradingOS daily summary",
        body=body,
        details={
            "tripped_kill_switch_modes": ",".join(tripped_modes) if tripped_modes else "none",
            "pending_approval_requests": pending_approvals,
            "pending_live_order_intents": pending_intents,
        },
    )
    logger.info(
        "notifications.daily_summary_sent",
        tripped_modes=tripped_modes,
        pending_approvals=pending_approvals,
        pending_intents=pending_intents,
    )


def start_notification_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(DAILY_SUMMARY_JOB_ID, run_daily_summary_job),
        CronTrigger(hour=DAILY_SUMMARY_HOUR_IST, minute=DAILY_SUMMARY_MINUTE_IST, timezone=IST),
        args=[session_factory],
        id=DAILY_SUMMARY_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler
