"""The single choke point for writing an audit log row (Build Spec §19) --
the same "one function, everyone calls it, nothing else may construct the
row" pattern this codebase already uses for RBAC (`src.core.rbac.
require_role`) and approvals (`src.orchestration.transitions.
conditional_transition`). `AuditLog` rows are never constructed directly
anywhere else in this codebase; every prior phase's ad hoc `AuditLog(...)`
call site (Phase 1's Agent Gateway apply/reject, Phase 8's Shadow Mode,
Phase 9's live trading) has been migrated to call `write_audit_entry`
instead, so those events are now genuinely part of the hash chain rather
than sitting in the same table as plain, unlinked rows.

**Why one GLOBAL chain, not one per entity/actor**: Build Spec §19 asks
for a single hash-chained audit log, not a per-entity ledger -- the whole
point is one continuous, independently-verifiable timeline across every
subsystem. Serialization uses a transaction-scoped Postgres advisory lock
(`pg_advisory_xact_lock`, auto-released at commit/rollback) keyed by a
fixed constant, the same gap-free-sequencing mechanism Phase 2's event bus
(`src.orchestration.events.emit`) already established for its own
per-run sequence numbers -- concurrent writers queue on the lock instead
of racing to read the same `MAX(sequence)` and either collide or skip a
number.

**Does not commit.** Every existing call site adds several rows (the
domain row plus the audit row) within one caller-managed transaction and
commits once at the end -- `write_audit_entry` only `add()`s and
`flush()`es, exactly matching that existing pattern, so migrating a call
site to it never changes when the transaction actually commits.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.chain import (
    GENESIS_HASH,
    AuditChainEntry,
    ChainVerificationResult,
    compute_entry_hash,
    verify_chain,
)
from src.models.audit_log import AuditLog

logger = structlog.get_logger(__name__)

# A fixed, arbitrary 63-bit constant (pg_advisory_xact_lock takes a signed
# bigint) -- every writer across the whole process locks on this same key,
# by design: there is exactly one chain.
_AUDIT_LOCK_KEY = 0x41554449544C4F47 & 0x7FFFFFFFFFFFFFFF


def _current_correlation_id() -> str | None:
    bound = structlog.contextvars.get_contextvars()
    value = bound.get("correlation_id")
    return str(value) if value is not None else None


async def write_audit_entry(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    details: dict | None = None,
    correlation_id: str | None = None,
) -> AuditLog:
    await db.execute(select(func.pg_advisory_xact_lock(_AUDIT_LOCK_KEY)))

    last = (
        await db.execute(
            select(AuditLog.sequence, AuditLog.hash).order_by(AuditLog.sequence.desc()).limit(1)
        )
    ).first()
    next_sequence = (last.sequence + 1) if last is not None else 1
    previous_hash = last.hash if last is not None else GENESIS_HASH

    created_at = datetime.now(UTC)
    resolved_correlation_id = (
        correlation_id if correlation_id is not None else _current_correlation_id()
    )

    entry_hash = compute_entry_hash(
        sequence=next_sequence,
        previous_hash=previous_hash,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        created_at=created_at,
    )

    row = AuditLog(
        id=uuid.uuid4(),
        sequence=next_sequence,
        previous_hash=previous_hash,
        hash=entry_hash,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        correlation_id=resolved_correlation_id,
        created_at=created_at,
    )
    db.add(row)
    await db.flush()
    return row


async def verify_db_chain(db: AsyncSession) -> ChainVerificationResult:
    """Recomputes and checks every row's hash directly from the live
    table -- the most direct possible proof that no code path (a stray
    direct SQL statement, a manually-dropped-and-reinstated trigger)
    altered a row's content after it was written. Independent of, and
    complementary to, `src.audit.archive.verify_archive_chain_divergence`,
    which compares the DB against the separate WORM copy.
    """
    rows = (await db.execute(select(AuditLog).order_by(AuditLog.sequence))).scalars().all()
    entries = [
        AuditChainEntry(
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
        for row in rows
    ]
    return verify_chain(entries)
