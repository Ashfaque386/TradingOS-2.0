"""WORM-style audit archive (Build Spec §19). **Choice made explicitly, per
the phase prompt's own "or a local append-only file store if MinIO is
overkill at this stage" allowance**: this codebase runs a local
append-only NDJSON file, not MinIO with Object Lock. Reasons, documented
rather than assumed:

1. Build Spec §5's own technology table already says as much: "`minio` |
   Audit archive (**optional at smallest scale**; can start with local
   append-only file storage and add MinIO later)".
2. This is a solo-operator deployment (Build Spec's own framing) -- a
   second object-storage service, its own container, its own credentials
   in the secrets store, and S3 API client code is real operational
   weight for a single archive file that a local disk (or, in production,
   a mounted network volume with its own backup policy) already serves.
3. Every other "MinIO vs simpler local alternative" decision point this
   build has hit (Phase 4's sandbox read-only mount, Phase 10's data lake)
   picked the locally-realizable option first and left a clean seam to
   swap in heavier infra later without a rewrite -- `ArchiveWriter`'s
   `append`/`read_all` shape below is that seam: a future MinIO-backed
   implementation satisfies the identical two methods.

**One continuous file, not daily rotation.** The whole point of a hash
chain is one unbroken timeline; splitting it across dated files would
fragment chain verification into N per-file checks instead of one
authoritative check across the whole archive. `audit_archive/audit_log.ndjson`
grows forever, one JSON object per line, in `sequence` order.

**WORM enforcement, honestly bounded.** After first creating the archive
file, this module attempts `chattr +a` (the ext4/xfs "append-only"
filesystem attribute -- even root cannot truncate or edit an append-only
file without first clearing the attribute, which itself requires
`CAP_LINUX_IMMUTABLE`). This is attempted, not assumed: many container
runtimes (this sandbox included) don't grant the capability `chattr`
needs, and the attempt fails silently into a logged warning rather than
raising -- application-level append-only discipline (this module never
opens the file in any mode but `"a"`) is what actually holds the archive
honest day to day; the OS-level attribute is defense in depth, applied
when the environment allows it, not the sole guarantee.
"""

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.chain import AuditChainEntry, verify_chain
from src.models.audit_log import AuditLog

logger = structlog.get_logger(__name__)

ARCHIVE_FILENAME = "audit_log.ndjson"


def _archive_path(archive_root: Path) -> Path:
    return archive_root / ARCHIVE_FILENAME


def _entry_to_json_line(entry: AuditChainEntry) -> str:
    return json.dumps(
        {
            "sequence": entry.sequence,
            "previous_hash": entry.previous_hash,
            "hash": entry.hash,
            "actor": entry.actor,
            "action": entry.action,
            "entity_type": entry.entity_type,
            "entity_id": entry.entity_id,
            "details": entry.details,
            "created_at": entry.created_at.isoformat(),
        },
        sort_keys=True,
    )


def _json_line_to_entry(line: str) -> AuditChainEntry:
    data = json.loads(line)
    return AuditChainEntry(
        sequence=data["sequence"],
        previous_hash=data["previous_hash"],
        hash=data["hash"],
        actor=data["actor"],
        action=data["action"],
        entity_type=data["entity_type"],
        entity_id=data["entity_id"],
        details=data["details"],
        created_at=datetime.fromisoformat(data["created_at"]),
    )


def _try_make_append_only(path: Path) -> bool:
    try:
        result = subprocess.run(
            ["chattr", "+a", str(path)], capture_output=True, timeout=5, check=False
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def read_all_archived_entries(archive_root: Path) -> list[AuditChainEntry]:
    path = _archive_path(archive_root)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(_json_line_to_entry(line))
    return entries


def last_archived_sequence(archive_root: Path) -> int:
    entries = read_all_archived_entries(archive_root)
    return entries[-1].sequence if entries else 0


@dataclass(frozen=True, slots=True)
class ArchivalSweepResult:
    entries_archived: int
    made_append_only: bool | None  # None: file already existed, attribute not re-attempted


def append_entries_to_archive(archive_root: Path, entries: list[AuditLog]) -> ArchivalSweepResult:
    if not entries:
        return ArchivalSweepResult(entries_archived=0, made_append_only=None)

    archive_root.mkdir(parents=True, exist_ok=True)
    path = _archive_path(archive_root)
    is_new_file = not path.exists()

    with path.open("a", encoding="utf-8") as f:
        for row in entries:
            chain_entry = AuditChainEntry(
                sequence=row.sequence,
                previous_hash=row.previous_hash,
                hash=row.hash,
                actor=row.actor,
                action=row.action,
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                details=row.details,
                created_at=row.created_at,
            )
            f.write(_entry_to_json_line(chain_entry) + "\n")

    made_append_only = _try_make_append_only(path) if is_new_file else None
    if is_new_file and not made_append_only:
        logger.warning("audit.archive_chattr_unavailable", path=str(path))

    return ArchivalSweepResult(entries_archived=len(entries), made_append_only=made_append_only)


async def run_archival_sweep(db: AsyncSession, archive_root: Path) -> ArchivalSweepResult:
    since_sequence = last_archived_sequence(archive_root)
    result = await db.execute(
        select(AuditLog).where(AuditLog.sequence > since_sequence).order_by(AuditLog.sequence)
    )
    new_rows = list(result.scalars().all())
    return append_entries_to_archive(archive_root, new_rows)


@dataclass(frozen=True, slots=True)
class DivergenceResult:
    diverged: bool
    archive_internally_valid: bool
    live_db_matches_archive: bool
    first_diverged_sequence: int | None
    reason: str | None


async def verify_archive_chain_divergence(db: AsyncSession, archive_root: Path) -> DivergenceResult:
    """Two independent checks, both must pass: the archive's own chain is
    self-consistent (nobody edited an archived line in place), AND the
    archive's content for every sequence it has actually matches the live
    DB's row for that same sequence (nobody tampered with either copy
    without the other noticing) -- either failing is reported as a real
    divergence with the exact first sequence it starts at.
    """
    archived = read_all_archived_entries(archive_root)
    if not archived:
        return DivergenceResult(
            diverged=False,
            archive_internally_valid=True,
            live_db_matches_archive=True,
            first_diverged_sequence=None,
            reason="archive is empty -- nothing to verify yet",
        )

    archive_check = verify_chain(archived)
    if not archive_check.valid:
        return DivergenceResult(
            diverged=True,
            archive_internally_valid=False,
            live_db_matches_archive=False,
            first_diverged_sequence=archive_check.first_broken_sequence,
            reason=f"archive copy's own chain is broken: {archive_check.reason}",
        )

    max_sequence = archived[-1].sequence
    live_rows = (
        (
            await db.execute(
                select(AuditLog)
                .where(AuditLog.sequence <= max_sequence)
                .order_by(AuditLog.sequence)
            )
        )
        .scalars()
        .all()
    )
    live_by_sequence = {row.sequence: row for row in live_rows}

    for entry in archived:
        live_row = live_by_sequence.get(entry.sequence)
        if live_row is None:
            return DivergenceResult(
                diverged=True,
                archive_internally_valid=True,
                live_db_matches_archive=False,
                first_diverged_sequence=entry.sequence,
                reason=f"sequence {entry.sequence} exists in the archive but not in the live DB",
            )
        if live_row.hash != entry.hash:
            return DivergenceResult(
                diverged=True,
                archive_internally_valid=True,
                live_db_matches_archive=False,
                first_diverged_sequence=entry.sequence,
                reason=(
                    f"sequence {entry.sequence}: archive hash {entry.hash!r} does not match "
                    f"live DB hash {live_row.hash!r}"
                ),
            )

    return DivergenceResult(
        diverged=False,
        archive_internally_valid=True,
        live_db_matches_archive=True,
        first_diverged_sequence=None,
        reason=None,
    )
