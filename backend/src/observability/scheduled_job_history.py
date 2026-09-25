"""Durable run-history for the real APScheduler jobs this app runs (Phase
22, docs/phase20-old-vs-new-comparison.md item 20). APScheduler itself
forgets a firing the moment it completes -- `attach_run_history_listener`
subscribes to each scheduler's `EVENT_JOB_EXECUTED`/`EVENT_JOB_ERROR`
events (fired synchronously on the same asyncio loop the scheduler runs
on -- true for every `AsyncIOScheduler` this app starts) and writes one
real `ScheduledJobRun` row per firing, which
`GET /api/v1/system/scheduled-jobs/{scheduler}/{job_id}/history` then
reads back.
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select

from src.models.scheduled_job_run import ScheduledJobRun, ScheduledJobRunStatus

logger = structlog.get_logger(__name__)

# Only the most recent N firings per (scheduler, job_id) are kept -- see
# ScheduledJobRun's docstring for why unbounded retention isn't needed here.
_MAX_RUNS_PER_JOB = 50


async def _record_run(
    db_session_factory: Callable, scheduler_name: str, event: JobExecutionEvent
) -> None:
    status = ScheduledJobRunStatus.FAILED if event.exception else ScheduledJobRunStatus.SUCCEEDED
    error = str(event.exception) if event.exception else None
    async with db_session_factory() as db:
        db.add(
            ScheduledJobRun(
                scheduler=scheduler_name,
                job_id=event.job_id,
                scheduled_run_time=event.scheduled_run_time,
                finished_at=datetime.now(UTC),
                status=status,
                error=error,
            )
        )
        stale_ids = (
            (
                await db.execute(
                    select(ScheduledJobRun.id)
                    .where(
                        ScheduledJobRun.scheduler == scheduler_name,
                        ScheduledJobRun.job_id == event.job_id,
                    )
                    .order_by(ScheduledJobRun.scheduled_run_time.desc())
                    .offset(_MAX_RUNS_PER_JOB)
                )
            )
            .scalars()
            .all()
        )
        if stale_ids:
            await db.execute(delete(ScheduledJobRun).where(ScheduledJobRun.id.in_(stale_ids)))
        await db.commit()


def attach_run_history_listener(
    scheduler: AsyncIOScheduler, scheduler_name: str, db_session_factory: Callable
) -> None:
    def _on_event(event: JobExecutionEvent) -> None:
        task = asyncio.create_task(_record_run(db_session_factory, scheduler_name, event))
        task.add_done_callback(
            lambda t: t.exception()
            and logger.error(
                "scheduled_job_history.write_failed",
                scheduler=scheduler_name,
                job_id=event.job_id,
                error=str(t.exception()),
            )
        )

    scheduler.add_listener(_on_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
