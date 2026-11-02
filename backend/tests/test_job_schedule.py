"""A schedule an administrator chose for a job is kept across a restart, and can be put back.

The rules (what a job may be set to) are `src.orchestration.job_schedule`; keeping, applying and
restoring it are `job_schedule_store` and the routes in `src.api.routes.system`.
"""

from datetime import UTC, datetime

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from httpx import AsyncClient
from sqlalchemy import func, select

from src.core.roles import Role
from src.models.audit_log import AuditLog
from src.models.scheduled_job_override import ScheduledJobOverride
from src.observability.scheduler_registry import register_scheduler, unregister_all
from src.orchestration.job_schedule import (
    ScheduleRefused,
    ScheduleSpec,
    check_schedule,
    default_for,
    forget_defaults,
    remember_default,
    remember_defaults,
    trigger_for,
    trigger_kind,
)
from src.orchestration.job_schedule_store import apply_stored_overrides

# ---------------------------------------------------------------- the rules


def test_an_interval_job_takes_seconds_and_nothing_else():
    assert check_schedule("interval", ScheduleSpec(seconds=60)) == ScheduleSpec(seconds=60)
    with pytest.raises(ScheduleRefused, match="fixed interval; supply 'seconds', not hour/minute"):
        check_schedule("interval", ScheduleSpec(seconds=60, hour=7))
    with pytest.raises(ScheduleRefused, match="fixed interval"):
        check_schedule("interval", ScheduleSpec(minute=5))
    for bad in (None, 0, -5):
        with pytest.raises(ScheduleRefused, match="seconds must be a positive integer"):
            check_schedule("interval", ScheduleSpec(seconds=bad))


def test_a_cron_job_takes_an_hour_and_a_minute_in_range_and_nothing_else():
    assert check_schedule("cron", ScheduleSpec(hour=0, minute=0)) == ScheduleSpec(hour=0, minute=0)
    assert check_schedule("cron", ScheduleSpec(hour=23, minute=59)).minute == 59
    with pytest.raises(ScheduleRefused, match="cron schedule; supply hour/minute, not seconds"):
        check_schedule("cron", ScheduleSpec(seconds=60, hour=7, minute=0))
    for spec in (ScheduleSpec(hour=7), ScheduleSpec(minute=0), ScheduleSpec()):
        with pytest.raises(ScheduleRefused, match="Both hour and minute are required"):
            check_schedule("cron", spec)
    for spec in (
        ScheduleSpec(hour=24, minute=0),
        ScheduleSpec(hour=-1, minute=0),
        ScheduleSpec(hour=7, minute=60),
        ScheduleSpec(hour=7, minute=-1),
    ):
        with pytest.raises(ScheduleRefused, match="hour must be 0-23 and minute must be 0-59"):
            check_schedule("cron", spec)


def test_the_kind_of_a_trigger_and_one_this_cannot_reschedule():
    assert trigger_kind(IntervalTrigger(seconds=5)) == "interval"
    assert trigger_kind(CronTrigger(hour=7)) == "cron"
    once = DateTrigger(run_date=datetime(2030, 1, 1, tzinfo=UTC))
    with pytest.raises(ScheduleRefused, match="Unsupported trigger type: DateTrigger"):
        trigger_kind(once)
    with pytest.raises(ScheduleRefused, match="Unsupported trigger type"):
        trigger_for(once, ScheduleSpec(seconds=5))


def test_a_cron_job_keeps_every_other_field_of_its_expression_and_its_time_zone():
    current = CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="Asia/Kolkata")

    changed = trigger_for(current, ScheduleSpec(hour=9, minute=30))

    assert isinstance(changed, CronTrigger)
    fields = {f.name: str(f) for f in changed.fields}
    assert fields["day_of_week"] == "mon" and fields["hour"] == "9" and fields["minute"] == "30"
    assert str(changed.timezone) == "Asia/Kolkata"


def test_an_interval_job_is_rescheduled_to_the_seconds_given_and_a_wrong_kind_is_refused():
    assert (
        trigger_for(IntervalTrigger(seconds=3600), ScheduleSpec(seconds=90)).interval_length == 90
    )
    with pytest.raises(ScheduleRefused):
        trigger_for(IntervalTrigger(seconds=3600), ScheduleSpec(hour=7, minute=0))
    with pytest.raises(ScheduleRefused):
        trigger_for(CronTrigger(hour=7), ScheduleSpec(seconds=30))


@pytest.mark.asyncio
async def test_a_job_s_own_schedule_is_remembered_once_and_the_first_one_wins():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, IntervalTrigger(seconds=60), id="job-a")
    scheduler.start()
    try:
        remember_defaults({"s": scheduler})
        scheduler.get_job("job-a").reschedule(trigger=IntervalTrigger(seconds=5))
        remember_defaults({"s": scheduler})  # the changed trigger must not replace the default

        assert default_for("s", "job-a").interval_length == 60
        assert remember_default("s", "job-a", IntervalTrigger(seconds=1)).interval_length == 60
        assert default_for("s", "unknown") is None
        forget_defaults()
        assert default_for("s", "job-a") is None
    finally:
        scheduler.shutdown(wait=False)


# ---------------------------------------------------------------- the routes and a restart


async def _admin(client: AsyncClient, make_user, email: str = "sched-admin@example.com"):
    await make_user(email, "supersecret1", Role.SYSTEM_ADMINISTRATOR)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "supersecret1"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _cron_scheduler(job_id: str = "weekly") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        lambda: None, CronTrigger(day_of_week="mon", hour=7, minute=0), id=job_id, name=job_id
    )
    scheduler.start()
    return scheduler


def _interval_scheduler(job_id: str = "often") -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: None, IntervalTrigger(seconds=3600), id=job_id, name=job_id)
    scheduler.start()
    return scheduler


URL = "/api/v1/system/scheduled-jobs/sched/{job}/schedule"


async def test_a_schedule_change_is_kept_and_the_list_says_which_job_is_customised(
    client: AsyncClient, make_user, db_session_factory
):
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        headers = await _admin(client, make_user)
        original = str(scheduler.get_job("weekly").trigger)

        saved = await client.put(
            URL.format(job="weekly"), json={"hour": 9, "minute": 30}, headers=headers
        )

        assert saved.status_code == 200, saved.text
        async with db_session_factory() as db:
            row = (await db.execute(select(ScheduledJobOverride))).scalar_one()
        assert (row.scheduler, row.job_id, row.hour, row.minute, row.seconds) == (
            "sched",
            "weekly",
            9,
            30,
            None,
        )
        assert row.updated_by == "sched-admin@example.com"
        listing = (await client.get("/api/v1/system/scheduled-jobs", headers=headers)).json()
        job = next(j for j in listing["jobs"] if j["job_id"] == "weekly")
        assert job["overridden"] is True
        assert job["default_trigger"] == original
        assert "hour='9'" in job["trigger"] and "day_of_week='mon'" in job["trigger"]
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_a_second_change_replaces_the_first_and_a_job_never_changed_is_not_customised(
    client: AsyncClient, make_user, db_session_factory
):
    changed, untouched = _cron_scheduler("weekly"), _interval_scheduler("often")
    register_scheduler("sched", changed)
    register_scheduler("other", untouched)
    try:
        headers = await _admin(client, make_user)
        await client.put(URL.format(job="weekly"), json={"hour": 9, "minute": 30}, headers=headers)
        await client.put(URL.format(job="weekly"), json={"hour": 11, "minute": 5}, headers=headers)

        async with db_session_factory() as db:
            rows = (await db.execute(select(ScheduledJobOverride))).scalars().all()
        assert [(r.hour, r.minute) for r in rows] == [(11, 5)]
        listing = (await client.get("/api/v1/system/scheduled-jobs", headers=headers)).json()
        by_job = {j["job_id"]: j for j in listing["jobs"]}
        assert by_job["weekly"]["overridden"] is True
        assert by_job["often"]["overridden"] is False and by_job["often"]["default_trigger"] is None
    finally:
        changed.shutdown(wait=False)
        untouched.shutdown(wait=False)
        unregister_all()


async def test_a_refused_schedule_is_not_kept(client: AsyncClient, make_user, db_session_factory):
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        headers = await _admin(client, make_user)
        refused = await client.put(
            URL.format(job="weekly"), json={"hour": 25, "minute": 0}, headers=headers
        )

        assert refused.status_code == 400
        async with db_session_factory() as db:
            assert (
                await db.execute(select(func.count()).select_from(ScheduledJobOverride))
            ).scalar_one() == 0
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_a_restart_applies_the_kept_schedule_on_top_of_the_codes_own(
    client: AsyncClient, make_user, db_session_factory
):
    first = _cron_scheduler()
    register_scheduler("sched", first)
    try:
        headers = await _admin(client, make_user)
        await client.put(URL.format(job="weekly"), json={"hour": 9, "minute": 30}, headers=headers)
    finally:
        first.shutdown(wait=False)
        unregister_all()

    # the process restarts: the code registers the job on its own schedule again
    forget_defaults()
    second = _cron_scheduler()
    try:
        before = str(second.get_job("weekly").trigger)
        applied = await apply_stored_overrides(db_session_factory, {"sched": second})

        after = second.get_job("weekly").trigger
        assert len(applied) == 1 and applied[0].startswith("sched/weekly: ")
        assert isinstance(after, CronTrigger)
        fields = {f.name: str(f) for f in after.fields}
        assert (fields["hour"], fields["minute"], fields["day_of_week"]) == ("9", "30", "mon")
        assert (
            str(default_for("sched", "weekly")) == before
        )  # the code's own is what a restore returns
    finally:
        second.shutdown(wait=False)


async def test_a_kept_schedule_for_a_job_that_is_gone_or_of_another_kind_is_left_alone(
    db_session_factory,
):
    async with db_session_factory() as db:
        db.add_all(
            [
                ScheduledJobOverride(
                    scheduler="sched", job_id="vanished", seconds=30, updated_by="x"
                ),
                ScheduledJobOverride(
                    scheduler="gone", job_id="weekly", hour=9, minute=0, updated_by="x"
                ),
                # an interval choice for a job that has since become a cron job
                ScheduledJobOverride(
                    scheduler="sched", job_id="weekly", seconds=30, updated_by="x"
                ),
            ]
        )
        await db.commit()
    scheduler = _cron_scheduler()
    try:
        before = str(scheduler.get_job("weekly").trigger)

        applied = await apply_stored_overrides(db_session_factory, {"sched": scheduler})

        assert applied == []
        assert str(scheduler.get_job("weekly").trigger) == before
    finally:
        scheduler.shutdown(wait=False)


async def test_restoring_puts_the_codes_schedule_back_forgets_the_choice_and_is_audited(
    client: AsyncClient, make_user, db_session_factory
):
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        headers = await _admin(client, make_user)
        original = str(scheduler.get_job("weekly").trigger)
        await client.put(URL.format(job="weekly"), json={"hour": 9, "minute": 30}, headers=headers)

        restored = await client.delete(URL.format(job="weekly"), headers=headers)

        assert restored.status_code == 200, restored.text
        assert restored.json()["trigger"] == original
        assert str(scheduler.get_job("weekly").trigger) == original
        async with db_session_factory() as db:
            assert (
                await db.execute(select(func.count()).select_from(ScheduledJobOverride))
            ).scalar_one() == 0
            entry = (
                await db.execute(
                    select(AuditLog).where(AuditLog.action == "scheduled_job.restore_schedule")
                )
            ).scalar_one()
        assert entry.actor == "sched-admin@example.com" and entry.entity_id == "sched/weekly"
        listing = (await client.get("/api/v1/system/scheduled-jobs", headers=headers)).json()
        assert next(j for j in listing["jobs"] if j["job_id"] == "weekly")["overridden"] is False
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_restoring_works_after_a_restart_because_the_default_was_remembered_first(
    client: AsyncClient, make_user, db_session_factory
):
    async with db_session_factory() as db:
        db.add(
            ScheduledJobOverride(
                scheduler="sched", job_id="weekly", hour=9, minute=30, updated_by="x"
            )
        )
        await db.commit()
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        original = str(scheduler.get_job("weekly").trigger)
        await apply_stored_overrides(db_session_factory, {"sched": scheduler})
        headers = await _admin(client, make_user)

        restored = await client.delete(URL.format(job="weekly"), headers=headers)

        assert restored.status_code == 200, restored.text
        assert str(scheduler.get_job("weekly").trigger) == original
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_a_job_already_on_its_own_schedule_is_refused_a_restore_and_nothing_is_audited(
    client: AsyncClient, make_user, db_session_factory
):
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        headers = await _admin(client, make_user)

        never_changed = await client.delete(URL.format(job="weekly"), headers=headers)
        await client.put(URL.format(job="weekly"), json={"hour": 9, "minute": 30}, headers=headers)
        await client.delete(URL.format(job="weekly"), headers=headers)
        twice = await client.delete(URL.format(job="weekly"), headers=headers)

        assert never_changed.status_code == 409 and twice.status_code == 409
        assert "nothing to restore" in never_changed.json()["detail"]
        async with db_session_factory() as db:
            restores = (
                await db.execute(
                    select(func.count())
                    .select_from(AuditLog)
                    .where(AuditLog.action == "scheduled_job.restore_schedule")
                )
            ).scalar_one()
        assert restores == 1  # the one that did something
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()


async def test_only_an_administrator_may_restore_and_an_unknown_job_is_a_404(
    client: AsyncClient, make_user
):
    scheduler = _cron_scheduler()
    register_scheduler("sched", scheduler)
    try:
        await make_user("sched-pm@example.com", "supersecret1", Role.PORTFOLIO_MANAGER)
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "sched-pm@example.com", "password": "supersecret1"}
        )
        pm = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        admin = await _admin(client, make_user)

        assert (await client.delete(URL.format(job="weekly"), headers=pm)).status_code == 403
        assert (await client.delete(URL.format(job="nope"), headers=admin)).status_code == 404
        missing = await client.delete(
            "/api/v1/system/scheduled-jobs/nowhere/weekly/schedule", headers=admin
        )
        assert missing.status_code == 404
    finally:
        scheduler.shutdown(wait=False)
        unregister_all()
