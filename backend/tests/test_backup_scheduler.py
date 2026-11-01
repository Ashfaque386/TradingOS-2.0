"""The nightly Postgres backup job (src.orchestration.backup_scheduler): what it keeps, and how a
person hears it did not happen.

`create_postgres_backup` is replaced by a fake that writes the same files; the real dump is
exercised in test_pg_backup.py.
"""

import asyncio
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from src.core.config import get_settings
from src.data.pg_backup import (
    PostgresBackupError,
    PostgresBackupManifest,
    dump_path,
    list_backups,
    manifest_path,
)
from src.models.engine_alert_event import EngineAlertEvent
from src.notifications.types import AlertLevel
from src.orchestration import backup_scheduler, engine_alerts
from src.orchestration.backup_scheduler import (
    BACKUP_ALERT_PREFIX,
    POSTGRES_BACKUP_JOB_ID,
    BackupAlreadyRunningError,
    backup_gauge_updater,
    run_postgres_backup,
    run_postgres_backup_job,
    start_backup_scheduler,
    update_backup_gauges,
)
from src.orchestration.engine_alerts import wait_for_announcements


def _manifest_for(backup_id: str) -> PostgresBackupManifest:
    return PostgresBackupManifest(
        backup_id=backup_id,
        created_at="x",
        dump_file=f"postgres-{backup_id}.dump",
        size_bytes=15,
        sha256="ab" * 32,
        alembic_revision="rev1",
        audit_tip_sequence=7,
        audit_tip_hash="cd" * 32,
        tables={"users": 2},
        toc_entries=40,
        toc_sha256="ef" * 32,
        pg_dump_version="pg_dump (PostgreSQL) 16.3",
        server_version="16.3",
    )


def _write_backup(root: Path, backup_id: str) -> PostgresBackupManifest:
    root.mkdir(parents=True, exist_ok=True)
    manifest = _manifest_for(backup_id)
    dump_path(root, backup_id).write_bytes(b"not a real dump")
    manifest_path(root, backup_id).write_text(
        json.dumps(asdict(manifest)),
        encoding="utf-8",
    )
    return manifest


def _stamp(days_ago: int) -> str:
    moment = datetime.now(UTC) - timedelta(days=days_ago, minutes=1)
    return moment.strftime("%Y%m%dT%H%M%SZ")


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_notify(level, *, title, body, details=None):
        calls.append((level, title, body, details))
        return []

    monkeypatch.setattr(engine_alerts, "notify", fake_notify)
    return calls


@pytest.fixture(autouse=True)
def _retention(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "postgres_backup_keep_count", 2)
    monkeypatch.setattr(settings, "postgres_backup_retention_days", 3)
    monkeypatch.setattr(settings, "postgres_backup_timeout_seconds", 99.0)


def _make_backups(monkeypatch, fail_with: Exception | None = None, *, delay: float = 0.0):
    """Replaces the dump: makes (or fails to make) a backup file set, and records how it was
    called."""
    calls: list[dict] = []

    async def fake_create(session_factory, root, *, database_url, timeout_seconds):
        calls.append({"root": root, "database_url": database_url, "timeout": timeout_seconds})
        if delay:
            await asyncio.sleep(delay)
        if fail_with is not None:
            raise fail_with
        return _write_backup(root, _stamp(0))

    monkeypatch.setattr(backup_scheduler, "create_postgres_backup", fake_create)
    return calls


async def _events(db_session_factory):
    async with db_session_factory() as db:
        return (
            (await db.execute(select(EngineAlertEvent).order_by(EngineAlertEvent.opened_at)))
            .scalars()
            .all()
        )


async def test_a_run_makes_a_backup_with_the_settings_and_prunes_what_retention_drops(
    db_session_factory, tmp_path, monkeypatch, sent
):
    calls = _make_backups(monkeypatch)
    old = {days: _stamp(days) for days in (5, 6, 7, 8)}  # four old ones; the new one makes five
    for stamp in old.values():
        _write_backup(tmp_path, stamp)

    run = await run_postgres_backup(db_session_factory, tmp_path, database_url="postgresql://x/y")

    assert calls == [{"root": tmp_path, "database_url": "postgresql://x/y", "timeout": 99.0}]
    assert run.manifest.backup_id == list_backups(tmp_path)[0].backup_id
    # the newest 2 are kept (the new one and the 5-day-old); nothing else is under 3 days old
    assert sorted(run.pruned) == sorted([old[6], old[7], old[8]])
    assert [b.backup_id for b in list_backups(tmp_path)] == [run.manifest.backup_id, old[5]]
    assert sent == []
    assert await _events(db_session_factory) == []


async def test_a_failed_backup_opens_a_critical_alert_and_says_so_to_the_caller(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, PostgresBackupError("backup: pg_dump exited with 1: disk full"))

    with pytest.raises(PostgresBackupError, match="disk full"):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")
    await wait_for_announcements()

    [event] = await _events(db_session_factory)
    assert (event.key, event.kind, event.severity, event.source) == (
        f"{BACKUP_ALERT_PREFIX}:failed",
        "postgres_backup_failed",
        "critical",
        "event",
    )
    assert event.title == "The Postgres backup failed"
    assert "disk full" in event.detail
    assert event.detail.endswith("there is nothing recent to restore the database from.")
    assert event.resolved_at is None
    assert [(level, title) for level, title, _, _ in sent] == [
        (AlertLevel.ENGINE, "The Postgres backup failed")
    ]


async def test_an_error_that_is_not_ours_is_reported_as_a_failed_backup(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, OSError("no space left on device"))

    with pytest.raises(PostgresBackupError, match="the backup failed: no space left on device"):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")

    [event] = await _events(db_session_factory)
    assert "no space left on device" in event.detail


async def test_the_same_failure_two_nights_running_is_one_alert_announced_once(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, PostgresBackupError("backup: disk full"))

    for _ in range(2):
        with pytest.raises(PostgresBackupError):
            await run_postgres_backup(db_session_factory, tmp_path, database_url="x")
        await wait_for_announcements()

    assert len(await _events(db_session_factory)) == 1
    assert len(sent) == 1


async def test_the_next_good_backup_closes_the_alert_with_a_cleared_notice(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, PostgresBackupError("backup: disk full"))
    with pytest.raises(PostgresBackupError):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")
    await wait_for_announcements()

    _make_backups(monkeypatch)
    await run_postgres_backup(db_session_factory, tmp_path, database_url="x")
    await wait_for_announcements()

    [event] = await _events(db_session_factory)
    assert event.resolved_at is not None
    assert [title for _, title, _, _ in sent] == [
        "The Postgres backup failed",
        "Cleared: The Postgres backup failed",
    ]


async def test_a_detail_that_is_very_long_is_cut_to_fit(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, PostgresBackupError("x" * 5000))

    with pytest.raises(PostgresBackupError):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")

    [event] = await _events(db_session_factory)
    assert len(event.detail) == 2000


async def test_a_second_backup_while_one_is_running_is_refused_and_leaves_the_first_alone(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, delay=0.2)

    first = asyncio.ensure_future(
        run_postgres_backup(db_session_factory, tmp_path, database_url="x")
    )
    await asyncio.sleep(0.05)
    with pytest.raises(BackupAlreadyRunningError, match="a backup is already running"):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")
    await first

    assert await _events(db_session_factory) == []  # the refusal is not a failed backup
    again = await run_postgres_backup(db_session_factory, tmp_path, database_url="x")  # free again
    assert again.manifest.backup_id


async def test_a_failure_does_not_leave_the_lock_held(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch, PostgresBackupError("backup: disk full"))
    with pytest.raises(PostgresBackupError, match="disk full"):
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")

    _make_backups(monkeypatch, PostgresBackupError("backup: still full"))
    with pytest.raises(PostgresBackupError, match="still full"):  # not BackupAlreadyRunningError
        await run_postgres_backup(db_session_factory, tmp_path, database_url="x")


async def test_the_job_runs_one_backup_and_lets_a_failure_reach_the_scheduler(
    db_session_factory, tmp_path, monkeypatch, sent
):
    _make_backups(monkeypatch)
    await run_postgres_backup_job(db_session_factory, tmp_path, "x")
    assert len(list_backups(tmp_path)) == 1

    _make_backups(monkeypatch, PostgresBackupError("backup: disk full"))
    with pytest.raises(PostgresBackupError):  # APScheduler records the failed run in its history
        await run_postgres_backup_job(db_session_factory, tmp_path, "x")


async def test_the_schedule_is_every_day_at_the_configured_ist_time(db_session_factory, tmp_path):
    scheduler = start_backup_scheduler(db_session_factory, root=tmp_path, database_url="x")
    try:
        [job] = scheduler.get_jobs()
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert job.id == POSTGRES_BACKUP_JOB_ID
        assert (fields["hour"], fields["minute"]) == ("22", "45")
        assert fields["day_of_week"] == "*"  # weekends too: the database changes either way
        assert str(job.trigger.timezone) == "Asia/Kolkata"
        assert job.args == (db_session_factory, tmp_path, "x")
        assert job.next_run_time is not None
    finally:
        scheduler.shutdown(wait=False)


# ---- the gauges Prometheus reads, for a night the job never ran ----


def _sample(name: str, kind: str):
    return REGISTRY.get_sample_value(name, {"kind": kind})


def _lake_backup(root: Path, hours_ago: float) -> None:
    moment = datetime.now(UTC) - timedelta(hours=hours_ago)
    directory = root / moment.strftime("%Y%m%dT%H%M%S%fZ")
    directory.mkdir(parents=True)
    entry = {"relative_path": "a.parquet", "sha256": "11" * 32, "row_count": 1, "size_bytes": 9}
    (directory / "manifest.json").write_text(
        json.dumps({"backup_id": directory.name, "created_at": "x", "files": [entry]}),
        encoding="utf-8",
    )


def _hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).strftime("%Y%m%dT%H%M%SZ")


def test_the_gauges_say_how_old_the_newest_backup_of_each_kind_is_and_whether_that_is_too_old(
    tmp_path,
):
    _write_backup(tmp_path / "pg", _hours_ago(3))
    _write_backup(tmp_path / "pg", _hours_ago(70))
    _lake_backup(tmp_path / "lake", 50)

    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_newest_ok_age_seconds", "postgres") == pytest.approx(
        3 * 3600, abs=60
    )
    assert _sample("tradingos_backup_stale", "postgres") == 0
    assert _sample("tradingos_backup_newest_ok_age_seconds", "lake") == pytest.approx(
        50 * 3600, abs=60
    )
    assert _sample("tradingos_backup_stale", "lake") == 1  # over the 36 hours, on its own


def test_the_limit_is_the_setting_not_a_number_in_the_rule(tmp_path, monkeypatch):
    _write_backup(tmp_path / "pg", _hours_ago(10))
    monkeypatch.setattr(get_settings(), "backup_stale_after_hours", 8.0)

    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_stale", "postgres") == 1


def test_an_unfinished_backup_is_not_the_newest_one(tmp_path):
    _write_backup(tmp_path / "pg", _hours_ago(30))
    dump_path(tmp_path / "pg", _hours_ago(1)).write_bytes(b"half")  # no manifest

    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_newest_ok_age_seconds", "postgres") == pytest.approx(
        30 * 3600, abs=60
    )


def test_with_no_backup_a_fresh_start_is_not_stale_but_a_long_run_is(tmp_path, monkeypatch):
    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_newest_ok_age_seconds", "postgres") is None  # nothing to age
    assert _sample("tradingos_backup_stale", "postgres") == 0  # just started: not yet late
    assert _sample("tradingos_backup_stale", "lake") == 0

    monkeypatch.setattr(backup_scheduler, "_STARTED_AT", time.monotonic() - 40 * 3600)
    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_stale", "postgres") == 1  # up for 40 hours, never backed up
    assert _sample("tradingos_backup_stale", "lake") == 1


def test_a_backup_made_after_a_stale_spell_clears_the_gauge_and_gives_back_an_age(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(backup_scheduler, "_STARTED_AT", time.monotonic() - 40 * 3600)
    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")
    assert _sample("tradingos_backup_stale", "postgres") == 1

    _write_backup(tmp_path / "pg", _hours_ago(0.01))
    update_backup_gauges(tmp_path / "pg", tmp_path / "lake")

    assert _sample("tradingos_backup_stale", "postgres") == 0
    assert _sample("tradingos_backup_newest_ok_age_seconds", "postgres") is not None


async def test_the_updater_sets_the_gauges_on_start_and_survives_a_failed_update(
    tmp_path, monkeypatch
):
    _write_backup(tmp_path / "pg", _hours_ago(2))
    task = backup_gauge_updater(tmp_path / "pg", tmp_path / "lake")
    try:
        await asyncio.sleep(0.3)
    finally:
        task.cancel()
    assert _sample("tradingos_backup_newest_ok_age_seconds", "postgres") == pytest.approx(
        2 * 3600, abs=60
    )

    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        raise OSError("the volume is not there")

    monkeypatch.setattr(backup_scheduler, "update_backup_gauges", failing)
    monkeypatch.setattr(backup_scheduler, "BACKUP_GAUGE_UPDATE_INTERVAL_SECONDS", 0.01)
    again = backup_gauge_updater(tmp_path / "pg", tmp_path / "lake")
    try:
        await asyncio.sleep(0.3)
    finally:
        again.cancel()
    assert len(calls) >= 2  # it kept going after the first failure
