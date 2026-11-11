"""The nightly Postgres backup (src.data.pg_backup), and the way a person hears it did not happen.

One job, every day (weekends too: the database changes whether or not the market is open) at 22:45
IST, after the lake backup at 22:15. It dumps the database, prunes what retention no longer keeps,
and -- the part that matters at 3 a.m. -- tells someone when it fails: a failed backup opens an
engine alert (critical: until the next one succeeds there is nothing recent to restore from), which
closes by itself, with a "cleared" notice, the night a backup goes through.

The same function serves the manual "Back up now" (POST /system/postgres-backup), so what a person
triggers is exactly what the schedule runs. Only one backup runs at a time.
"""

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import get_settings
from src.data.backup import list_lake_backups
from src.data.pg_backup import (
    PostgresBackupError,
    PostgresBackupManifest,
    backup_is_stale,
    create_postgres_backup,
    list_backups,
    prune_backups,
)
from src.engine.paper_trading.market_hours import IST
from src.observability.correlation import with_job_correlation_id
from src.observability.metrics import backup_newest_ok_age_seconds, backup_stale
from src.orchestration.engine_alerts import AlertCandidate, sync_conditions_safely

logger = structlog.get_logger(__name__)

POSTGRES_BACKUP_JOB_ID = "postgres_backup"
BACKUP_ALERT_PREFIX = "backup:postgres"

BACKUP_GAUGE_UPDATE_INTERVAL_SECONDS = 300

_running = False
_STARTED_AT = time.monotonic()


class BackupAlreadyRunningError(PostgresBackupError):
    """A backup is already being made; a second one would only slow it down."""


@dataclass(frozen=True, slots=True)
class BackupRun:
    manifest: PostgresBackupManifest
    pruned: list[str]


async def run_postgres_backup(
    session_factory: async_sessionmaker[AsyncSession], root: Path, *, database_url: str
) -> BackupRun:
    """Makes a backup, prunes by the retention settings, and keeps the engine alert in step with
    the outcome. Raises `PostgresBackupError` (after opening the alert) when it fails."""
    global _running
    if _running:
        raise BackupAlreadyRunningError("a backup is already running")
    _running = True
    settings = get_settings()
    try:
        try:
            manifest = await create_postgres_backup(
                session_factory,
                root,
                database_url=database_url,
                timeout_seconds=settings.postgres_backup_timeout_seconds,
            )
        except Exception as exc:
            logger.exception("pg_backup.failed")
            async with session_factory() as db:
                await sync_conditions_safely(
                    db,
                    source="event",
                    prefix=BACKUP_ALERT_PREFIX,
                    present=[
                        AlertCandidate(
                            key=f"{BACKUP_ALERT_PREFIX}:failed",
                            kind="postgres_backup_failed",
                            severity="critical",
                            title="The Postgres backup failed",
                            detail=(
                                f"{exc} Until a backup succeeds there is nothing recent to restore "
                                "the database from."
                            )[:2000],
                        )
                    ],
                )
            if isinstance(exc, PostgresBackupError):
                raise
            raise PostgresBackupError(f"the backup failed: {exc}") from exc
        pruned = prune_backups(
            root,
            keep_count=settings.postgres_backup_keep_count,
            retention_days=settings.postgres_backup_retention_days,
        )
        async with session_factory() as db:
            await sync_conditions_safely(db, source="event", prefix=BACKUP_ALERT_PREFIX, present=[])
        return BackupRun(manifest=manifest, pruned=pruned)
    finally:
        _running = False


async def run_postgres_backup_job(
    session_factory: async_sessionmaker[AsyncSession], root: Path, database_url: str
) -> None:
    run = await run_postgres_backup(session_factory, root, database_url=database_url)
    logger.info(
        "pg_backup.job_ran",
        backup_id=run.manifest.backup_id,
        size_bytes=run.manifest.size_bytes,
        pruned=run.pruned,
    )


def start_backup_scheduler(
    session_factory: async_sessionmaker[AsyncSession], *, root: Path, database_url: str
) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        with_job_correlation_id(POSTGRES_BACKUP_JOB_ID, run_postgres_backup_job),
        CronTrigger(
            hour=settings.postgres_backup_hour_ist,
            minute=settings.postgres_backup_minute_ist,
            timezone=IST,
        ),
        args=[session_factory, root, database_url],
        id=POSTGRES_BACKUP_JOB_ID,
        replace_existing=True,
    )
    scheduler.start()
    return scheduler


# A night the process was not running at 22:45 IST makes no backup, and APScheduler does not run a
# cron job for a time that passed while it was down. So, once the app has finished starting, a
# backup more than a day old is made up for. With no backup at all nothing is owed: a fresh install
# makes its first one at the scheduled time (and the end-to-end tests start from nothing).
CATCH_UP_JOB_NAME = "postgres_backup_catch_up"
CATCH_UP_DELAY_SECONDS = 120.0
CATCH_UP_AFTER_SECONDS = 24 * 3600 + 300  # a day, and a few minutes for the dump's own length


def backup_is_owed(root: Path, now: datetime) -> bool:
    """Whether a scheduled night was missed: the newest usable backup is more than a day old."""
    newest = next((info for info in list_backups(root) if info.status == "ok"), None)
    if newest is None:
        return False
    return (now - newest.created_at).total_seconds() > CATCH_UP_AFTER_SECONDS


async def _catch_up(
    session_factory: async_sessionmaker[AsyncSession],
    root: Path,
    database_url: str,
    delay_seconds: float,
) -> None:
    await asyncio.sleep(delay_seconds)
    # Judged after the wait, not at boot: a backup made in between (by hand) settles it.
    if not await asyncio.to_thread(backup_is_owed, root, datetime.now(UTC)):
        return
    logger.info("pg_backup.catching_up_a_missed_night")
    try:
        await with_job_correlation_id(CATCH_UP_JOB_NAME, run_postgres_backup_job)(
            session_factory, root, database_url
        )
    except BackupAlreadyRunningError:
        return  # someone pressed "Back up now" first
    except Exception:  # noqa: BLE001 - the failure is already an engine alert; never crash startup
        logger.exception("pg_backup.catch_up_failed")


def backup_catch_up(
    session_factory: async_sessionmaker[AsyncSession],
    root: Path,
    *,
    database_url: str,
    delay_seconds: float = CATCH_UP_DELAY_SECONDS,
) -> asyncio.Task[None]:
    """Starts the one-off check described above; the caller cancels the task at shutdown."""
    return asyncio.create_task(_catch_up(session_factory, root, database_url, delay_seconds))


def update_backup_gauges(
    postgres_root: Path, lake_root: Path, *, now: datetime | None = None
) -> None:
    """Sets the age and staleness gauges for both kinds of backup from what is on disk. With no
    usable backup at all the system has gone without one for as long as this process has run, so a
    fresh install is not called stale until it has been up for the limit."""
    moment = now or datetime.now(UTC)
    limit = get_settings().backup_stale_after_hours
    kinds = (("postgres", list_backups(postgres_root)), ("lake", list_lake_backups(lake_root)))
    for kind, infos in kinds:
        newest = next((info for info in infos if info.status == "ok"), None)
        if newest is None:
            try:
                backup_newest_ok_age_seconds.remove(kind)
            except KeyError:  # nothing was ever set for it
                pass
            judged = time.monotonic() - _STARTED_AT
        else:
            judged = max(0.0, (moment - newest.created_at).total_seconds())
            backup_newest_ok_age_seconds.labels(kind).set(judged)
        backup_stale.labels(kind).set(1 if backup_is_stale(judged, limit) else 0)


async def _backup_gauge_loop(postgres_root: Path, lake_root: Path) -> None:
    while True:
        try:
            await asyncio.to_thread(update_backup_gauges, postgres_root, lake_root)
        except Exception:  # noqa: BLE001 - a metrics update must never crash the loop
            logger.exception("pg_backup.gauge_update_failed")
        await asyncio.sleep(BACKUP_GAUGE_UPDATE_INTERVAL_SECONDS)


def backup_gauge_updater(postgres_root: Path, lake_root: Path) -> asyncio.Task[None]:
    return asyncio.create_task(_backup_gauge_loop(postgres_root, lake_root))
