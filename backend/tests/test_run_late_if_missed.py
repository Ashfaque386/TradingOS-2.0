"""A nightly housekeeping job the host slept through runs when it wakes (`RUN_LATE_IF_MISSED`).

The process keeps running while the machine sleeps, so on waking APScheduler finds the run time
hours behind. With its default options it skips the run ("was missed by ..."); seen live, 12-17 Nov
2026, as six nights without a Postgres backup.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.orchestration import market_data_scheduler as market_data_module
from src.orchestration.backup_scheduler import POSTGRES_BACKUP_JOB_ID, start_backup_scheduler
from src.orchestration.job_schedule import (
    RUN_LATE_GRACE_SECONDS,
    RUN_LATE_IF_MISSED,
    ScheduleSpec,
    trigger_for,
)

HOUSEKEEPING = {market_data_module.CATALOG_REFRESH_JOB_ID, market_data_module.BACKUP_JOB_ID}


async def _runs_after_waking(missed_by: timedelta, every: timedelta, **options) -> int:
    """How many times a job runs when the scheduler starts with its run time `missed_by` ago --
    the same thing a scheduler sees when the host wakes."""
    runs: list[int] = []
    scheduler = AsyncIOScheduler(timezone=UTC)
    scheduler.add_job(
        lambda: runs.append(1),
        IntervalTrigger(seconds=every.total_seconds()),
        next_run_time=datetime.now(UTC) - missed_by,
        **options,
    )
    scheduler.start()
    try:
        await asyncio.sleep(0.5)
    finally:
        scheduler.shutdown(wait=False)
    return len(runs)


async def test_by_default_a_run_missed_while_the_host_slept_is_skipped():
    assert await _runs_after_waking(timedelta(hours=3), timedelta(days=1)) == 0


async def test_with_the_option_it_runs_on_waking():
    runs = await _runs_after_waking(timedelta(hours=3), timedelta(days=1), **RUN_LATE_IF_MISSED)
    assert runs == 1


async def test_several_missed_runs_become_one():
    # every 8 h, slept 20 h: three run times passed (-20 h, -12 h, -4 h), all inside the grace
    runs = await _runs_after_waking(timedelta(hours=20), timedelta(hours=8), **RUN_LATE_IF_MISSED)
    assert runs == 1


async def test_a_run_missed_by_more_than_the_grace_is_still_skipped():
    missed_by = timedelta(seconds=RUN_LATE_GRACE_SECONDS + 3600)
    runs = await _runs_after_waking(missed_by, timedelta(days=3), **RUN_LATE_IF_MISSED)
    assert runs == 0


def _options(job) -> tuple[float | None, bool]:
    return job.misfire_grace_time, job.coalesce


async def test_the_postgres_backup_runs_late(db_session_factory, tmp_path):
    scheduler = start_backup_scheduler(db_session_factory, root=tmp_path, database_url="x")
    try:
        job = scheduler.get_job(POSTGRES_BACKUP_JOB_ID)
        assert _options(job) == (RUN_LATE_GRACE_SECONDS, True)
    finally:
        scheduler.shutdown(wait=False)


async def test_only_the_market_data_housekeeping_runs_late():
    """Ingestion reads "today" when it runs, so run after midnight it would ingest the wrong day;
    those jobs keep APScheduler's default and are skipped when missed."""
    scheduler = market_data_module.start_market_data_scheduler(
        None,  # type: ignore[arg-type]
        provider=None,  # type: ignore[arg-type]
        root=Path("unused"),
        backup_root=Path("unused"),
    )
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)

    assert HOUSEKEEPING <= jobs.keys()
    for job_id, job in jobs.items():
        if job_id in HOUSEKEEPING:
            assert _options(job) == (RUN_LATE_GRACE_SECONDS, True), job_id
        else:
            assert job.misfire_grace_time == 1, job_id


async def test_a_schedule_change_keeps_the_option(db_session_factory, tmp_path):
    scheduler = start_backup_scheduler(db_session_factory, root=tmp_path, database_url="x")
    try:
        job = scheduler.get_job(POSTGRES_BACKUP_JOB_ID)
        new_trigger = trigger_for(job.trigger, ScheduleSpec(hour=23, minute=10))
        job.reschedule(trigger=new_trigger)  # as the schedule route and the restart do
        job = scheduler.get_job(POSTGRES_BACKUP_JOB_ID)
        assert {f.name: str(f) for f in job.trigger.fields}["hour"] == "23"
        assert _options(job) == (RUN_LATE_GRACE_SECONDS, True)
    finally:
        scheduler.shutdown(wait=False)
