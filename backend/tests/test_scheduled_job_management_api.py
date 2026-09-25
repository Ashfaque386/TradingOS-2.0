"""Scheduled-job run-now / edit-schedule / run-history (Phase 22,
docs/phase20-old-vs-new-comparison.md item 20): the live-mutation
endpoints act on a real `AsyncIOScheduler` registered the same way
`tests/test_api_system.py::test_scheduled_jobs_reports_real_live_state_from_the_registry`
already does, and the history endpoint reads real rows written by
`src.observability.scheduled_job_history`'s execution listener.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from httpx import AsyncClient
from sqlalchemy import select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.scheduled_job_run import ScheduledJobRun, ScheduledJobRunStatus
from src.observability.scheduled_job_history import attach_run_history_listener
from src.observability.scheduler_registry import register_scheduler, unregister_all


async def _login(client: AsyncClient, email: str, password: str) -> str:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _make_scheduler_with_cron_job(job_id: str = "fake_cron_job") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: None,
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id=job_id,
        name="fake cron",
    )
    scheduler.start()
    return scheduler


async def _make_scheduler_with_interval_job(job_id: str = "fake_interval_job") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, IntervalTrigger(seconds=3600), id=job_id, name="fake interval")
    scheduler.start()
    return scheduler


async def test_run_now_requires_system_administrator(client: AsyncClient, make_user):
    scheduler = await _make_scheduler_with_cron_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("runnow-pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        token = await _login(client, "runnow-pm@example.com", "supersecret1")
        resp = await client.post(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/run-now",
            headers=_auth(token),
        )
        assert resp.status_code == 403
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_run_now_moves_next_run_time_to_now_without_disturbing_the_cadence(
    client: AsyncClient, make_user, db_session_factory
):
    # A job function that records when it actually ran, rather than
    # asserting `next_run_time` lands near "now": APScheduler's own
    # background loop can pick up and complete a now-due job before this
    # handler even reads the job back, at which point a CRON trigger has
    # already advanced past "now" to its real next match (next Monday) --
    # correct behavior, not a race to paper over with a wider tolerance.
    executions: list[datetime] = []
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: executions.append(datetime.now(UTC)),
        CronTrigger(day_of_week="mon", hour=7, minute=0),
        id="fake_cron_job",
        name="fake cron",
    )
    scheduler.start()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("runnow-admin@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "runnow-admin@example.com", "supersecret1")

        resp = await client.post(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/run-now",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["scheduler"] == "fake_scheduler"
        assert body["job_id"] == "fake_cron_job"

        for _ in range(30):
            if executions:
                break
            await asyncio.sleep(0.1)
        assert executions, "run-now did not cause the job to actually fire"

        # The job's regular cron cadence (Monday 07:00) is untouched --
        # only this one firing moved, not the trigger itself.
        job = scheduler.get_job("fake_cron_job")
        assert isinstance(job.trigger, CronTrigger)
        assert "day_of_week='mon'" in str(job.trigger)

        # A regression check for a real bug: the explicit write_audit_entry
        # call in the route handler was never followed by db.commit(), so
        # this row was silently discarded when the request-scoped session
        # closed -- only the generic AuditLoggingMiddleware entry ever
        # actually persisted. Reading it back here (rather than trusting
        # that the call was made) is what would have caught it.
        async with db_session_factory() as db:
            result = await db.execute(
                select(AuditLog).where(AuditLog.action == "scheduled_job.run_now")
            )
            row = result.scalars().first()
        assert row is not None, "scheduled_job.run_now audit entry was never committed"
        assert row.entity_id == "fake_scheduler/fake_cron_job"
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_run_now_unknown_scheduler_or_job_is_404(client: AsyncClient, make_user):
    await make_user("runnow-404@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    token = await _login(client, "runnow-404@example.com", "supersecret1")

    resp = await client.post(
        "/api/v1/system/scheduled-jobs/no_such_scheduler/no_such_job/run-now",
        headers=_auth(token),
    )
    assert resp.status_code == 404


async def test_reschedule_interval_job_accepts_seconds(client: AsyncClient, make_user):
    scheduler = await _make_scheduler_with_interval_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("resched-admin1@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "resched-admin1@example.com", "supersecret1")

        resp = await client.put(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_interval_job/schedule",
            headers=_auth(token),
            json={"seconds": 120},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "0:02:00" in body["trigger"]
        job = scheduler.get_job("fake_interval_job")
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(seconds=120)
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_reschedule_interval_job_rejects_hour_minute(client: AsyncClient, make_user):
    scheduler = await _make_scheduler_with_interval_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("resched-admin2@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "resched-admin2@example.com", "supersecret1")

        resp = await client.put(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_interval_job/schedule",
            headers=_auth(token),
            json={"hour": 7, "minute": 0},
        )
        assert resp.status_code == 400
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_reschedule_cron_job_changes_hour_minute_but_preserves_day_of_week(
    client: AsyncClient, make_user, db_session_factory
):
    scheduler = await _make_scheduler_with_cron_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("resched-admin3@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "resched-admin3@example.com", "supersecret1")

        resp = await client.put(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/schedule",
            headers=_auth(token),
            json={"hour": 9, "minute": 30},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "hour='9'" in body["trigger"]
        assert "minute='30'" in body["trigger"]
        # The field this edit never mentioned must survive unchanged.
        assert "day_of_week='mon'" in body["trigger"]

        # Same regression check as run-now's own test above: this write
        # must actually be committed, not just constructed and discarded.
        async with db_session_factory() as db:
            result = await db.execute(
                select(AuditLog).where(AuditLog.action == "scheduled_job.reschedule")
            )
            row = result.scalars().first()
        assert row is not None, "scheduled_job.reschedule audit entry was never committed"
        assert row.details is not None
        assert "day_of_week='mon'" in row.details["old_trigger"]
        assert "hour='9'" in row.details["new_trigger"]
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_reschedule_cron_job_rejects_out_of_range_hour(client: AsyncClient, make_user):
    scheduler = await _make_scheduler_with_cron_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("resched-admin4@example.com", "supersecret1", Role.SYSTEM_ADMINISTRATOR)
        token = await _login(client, "resched-admin4@example.com", "supersecret1")

        resp = await client.put(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/schedule",
            headers=_auth(token),
            json={"hour": 25, "minute": 0},
        )
        assert resp.status_code == 400
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_reschedule_requires_system_administrator(client: AsyncClient, make_user):
    scheduler = await _make_scheduler_with_cron_job()
    register_scheduler("fake_scheduler", scheduler)
    try:
        await make_user("resched-ro@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
        token = await _login(client, "resched-ro@example.com", "supersecret1")

        resp = await client.put(
            "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/schedule",
            headers=_auth(token),
            json={"hour": 9, "minute": 30},
        )
        assert resp.status_code == 403
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_history_endpoint_returns_seeded_rows_most_recent_first(
    client: AsyncClient, make_user, db_session_factory
):
    now = datetime.now(UTC)
    async with db_session_factory() as db:
        db.add(
            ScheduledJobRun(
                scheduler="fake_scheduler",
                job_id="fake_cron_job",
                scheduled_run_time=now - timedelta(hours=1),
                finished_at=now - timedelta(hours=1),
                status=ScheduledJobRunStatus.SUCCEEDED,
                error=None,
            )
        )
        db.add(
            ScheduledJobRun(
                scheduler="fake_scheduler",
                job_id="fake_cron_job",
                scheduled_run_time=now,
                finished_at=now,
                status=ScheduledJobRunStatus.FAILED,
                error="boom",
            )
        )
        await db.commit()

    await make_user("history-viewer@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "history-viewer@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/history",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    runs = resp.json()["runs"]
    assert len(runs) == 2
    assert runs[0]["status"] == "failed"
    assert runs[0]["error"] == "boom"
    assert runs[1]["status"] == "succeeded"
    assert runs[1]["error"] is None


async def test_history_endpoint_requires_authentication(client: AsyncClient):
    resp = await client.get("/api/v1/system/scheduled-jobs/fake_scheduler/fake_cron_job/history")
    assert resp.status_code in (401, 403)


async def test_history_endpoint_empty_for_a_job_with_no_recorded_firings(
    client: AsyncClient, make_user
):
    await make_user("history-empty@example.com", "supersecret1", Role.READ_ONLY_AUDITOR)
    token = await _login(client, "history-empty@example.com", "supersecret1")

    resp = await client.get(
        "/api/v1/system/scheduled-jobs/never_registered/never_fired/history",
        headers=_auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["runs"] == []


async def _run_one_job_and_await_its_history_write(
    monkeypatch, db_session_factory, *, job_fn, job_id: str
) -> ScheduledJobRun | None:
    """`attach_run_history_listener`'s callback fires the write via
    `asyncio.create_task` (APScheduler's own event callback is sync, so it
    can't be awaited directly) -- fire-and-forget in production, but a test
    must wait for that specific write to actually finish before it returns,
    or the write's still-open DB connection can race the NEXT test's
    `db_session_factory` fixture doing `drop_all`/`create_all` on the same
    shared database, which manifests as a Postgres deadlock in a completely
    unrelated later test. Tracking and awaiting the exact task this
    listener created (rather than polling and hoping, or awaiting every
    pending task -- the scheduler's own internal wakeup timer never
    finishes until shutdown) makes the wait deterministic.
    """
    created_tasks: list[asyncio.Task] = []
    real_create_task = asyncio.create_task

    def _tracking_create_task(coro, *args, **kwargs):
        task = real_create_task(coro, *args, **kwargs)
        created_tasks.append(task)
        return task

    monkeypatch.setattr(
        "src.observability.scheduled_job_history.asyncio.create_task", _tracking_create_task
    )

    scheduler = AsyncIOScheduler()
    attach_run_history_listener(scheduler, "listener_test_scheduler", db_session_factory)
    scheduler.add_job(job_fn, "date", run_date=datetime.now(UTC), id=job_id)
    scheduler.start()
    try:
        for _ in range(50):
            if created_tasks:
                break
            await asyncio.sleep(0.1)
        assert created_tasks, "listener never scheduled a history write within 5s"
        await asyncio.gather(*created_tasks)
    finally:
        scheduler.shutdown(wait=False)

    from sqlalchemy import select

    async with db_session_factory() as db:
        result = await db.execute(select(ScheduledJobRun).where(ScheduledJobRun.job_id == job_id))
        return result.scalars().first()


async def test_execution_listener_writes_a_real_row_on_successful_firing(
    db_session_factory, monkeypatch
):
    row = await _run_one_job_and_await_its_history_write(
        monkeypatch, db_session_factory, job_fn=lambda: None, job_id="listener_success_job"
    )
    assert row is not None
    assert row.status == ScheduledJobRunStatus.SUCCEEDED
    assert row.error is None


async def test_execution_listener_writes_a_failed_row_when_the_job_raises(
    db_session_factory, monkeypatch
):
    def _boom():
        raise ValueError("deliberate failure")

    row = await _run_one_job_and_await_its_history_write(
        monkeypatch, db_session_factory, job_fn=_boom, job_id="listener_failure_job"
    )
    assert row is not None
    assert row.status == ScheduledJobRunStatus.FAILED
    assert "deliberate failure" in row.error
