"""Autonomous scheduling for the audit archive (Build Spec §19): a frequent
archival sweep (append any new `audit_log` rows to the WORM file) and a
less frequent chain-divergence verification, mirroring every other
phase's APScheduler shape (Phase 7/9/10). Both run unconditionally --
unlike the market-data/paper/live-trading schedulers, nothing here is
market-hours-gated, since audit activity (and the need to catch tampering)
doesn't stop when NSE closes.

A divergence finding is itself written back as an audit entry
(`audit.divergence_detected`) via `write_audit_entry` -- the one
self-referential case in this codebase where the audit system audits
itself: if the *live DB* was somehow tampered (a superuser dropping and
later restoring the append-only trigger to sneak an `UPDATE` through, for
instance), that new entry still lands after the tampering, in the clear,
forming a permanent "we caught it and when" marker in the timeline going
forward -- exactly what a real incident response needs.
"""

from pathlib import Path

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.audit.archive import run_archival_sweep, verify_archive_chain_divergence
from src.audit.service import write_audit_entry
from src.observability.correlation import with_job_correlation_id

logger = structlog.get_logger(__name__)

ARCHIVAL_SWEEP_INTERVAL_SECONDS = 300
DIVERGENCE_CHECK_INTERVAL_SECONDS = 3600

ARCHIVAL_SWEEP_JOB_ID = "audit_archival_sweep"
DIVERGENCE_CHECK_JOB_ID = "audit_divergence_check"


async def run_archival_sweep_job(
    session_factory: async_sessionmaker[AsyncSession], archive_root: Path
) -> None:
    async with session_factory() as db:
        result = await run_archival_sweep(db, archive_root)
    if result.entries_archived:
        logger.info("audit.archival_sweep_ran", entries_archived=result.entries_archived)


async def run_divergence_check_job(
    session_factory: async_sessionmaker[AsyncSession], archive_root: Path
) -> None:
    async with session_factory() as db:
        result = await verify_archive_chain_divergence(db, archive_root)
        if result.diverged:
            logger.error(
                "audit.chain_divergence_detected",
                first_diverged_sequence=result.first_diverged_sequence,
                reason=result.reason,
            )
            await write_audit_entry(
                db,
                actor="system",
                action="audit.divergence_detected",
                entity_type="audit_log",
                entity_id=str(result.first_diverged_sequence),
                details={
                    "archive_internally_valid": result.archive_internally_valid,
                    "live_db_matches_archive": result.live_db_matches_archive,
                    "reason": result.reason,
                },
            )
            await db.commit()
        else:
            logger.info("audit.divergence_check_clean")


def start_audit_scheduler(
    session_factory: async_sessionmaker[AsyncSession], *, archive_root: Path
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        with_job_correlation_id(ARCHIVAL_SWEEP_JOB_ID, run_archival_sweep_job),
        IntervalTrigger(seconds=ARCHIVAL_SWEEP_INTERVAL_SECONDS),
        args=[session_factory, archive_root],
        id=ARCHIVAL_SWEEP_JOB_ID,
        replace_existing=True,
    )
    scheduler.add_job(
        with_job_correlation_id(DIVERGENCE_CHECK_JOB_ID, run_divergence_check_job),
        IntervalTrigger(seconds=DIVERGENCE_CHECK_INTERVAL_SECONDS),
        args=[session_factory, archive_root],
        id=DIVERGENCE_CHECK_JOB_ID,
        replace_existing=True,
    )

    scheduler.start()
    return scheduler
