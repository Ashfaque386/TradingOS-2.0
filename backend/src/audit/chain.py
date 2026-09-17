"""Pure hash-chain functions for the audit log (Build Spec §19): each row's
`hash` is a SHA-256 over its own content plus the previous row's `hash`, so
altering, reordering, or deleting any row (even via a hypothetical direct
DB write that somehow bypassed the append-only trigger) breaks every
subsequent row's chain link, not just the tampered one.

Kept dependency-free from SQLAlchemy on purpose (same posture as
`src.engine.backtest.friction`): `AuditChainEntry` is a plain dataclass, so
`verify_chain` can validate rows pulled from the live DB (via
`src.audit.service`) or rows read back from a WORM archive NDJSON file
(via `src.audit.archive`) with the exact same code path -- a
divergence-check is nothing more than running the same verification twice
against two different sources and comparing.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

# Sequence 1's previous_hash -- a fixed, documented genesis marker, not a
# real hash of anything. Distinguishing "first row" from "a row whose
# previous link is missing/corrupted" is exactly the point: any row whose
# stored previous_hash isn't ether this genesis value or the prior row's
# real hash is provably broken.
GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class AuditChainEntry:
    sequence: int
    previous_hash: str
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict | None
    created_at: datetime
    hash: str


def compute_entry_hash(
    *,
    sequence: int,
    previous_hash: str,
    actor: str,
    action: str,
    entity_type: str | None,
    entity_id: str | None,
    details: dict | None,
    created_at: datetime,
) -> str:
    canonical_details = (
        json.dumps(details, sort_keys=True, default=str) if details is not None else "null"
    )
    payload = "|".join(
        [
            str(sequence),
            previous_hash,
            actor,
            action,
            entity_type or "",
            entity_id or "",
            canonical_details,
            created_at.isoformat(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChainVerificationResult:
    valid: bool
    entries_checked: int
    first_broken_sequence: int | None = None
    reason: str | None = None


def verify_chain(entries: list[AuditChainEntry]) -> ChainVerificationResult:
    """`entries` must already be sorted by `sequence` ascending (both the
    live-DB reader and the archive reader guarantee this). Checks two
    things per row: its own `hash` recomputes correctly from its content,
    and its `previous_hash` matches the prior row's actual `hash` (or
    `GENESIS_HASH` for the first row) -- either failing means the chain
    is broken from that point on.
    """
    expected_previous = GENESIS_HASH
    for entry in entries:
        if entry.previous_hash != expected_previous:
            return ChainVerificationResult(
                valid=False,
                entries_checked=entry.sequence,
                first_broken_sequence=entry.sequence,
                reason=(
                    f"sequence {entry.sequence}: previous_hash {entry.previous_hash!r} "
                    f"does not match the prior row's hash {expected_previous!r}"
                ),
            )
        recomputed = compute_entry_hash(
            sequence=entry.sequence,
            previous_hash=entry.previous_hash,
            actor=entry.actor,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            details=entry.details,
            created_at=entry.created_at,
        )
        if recomputed != entry.hash:
            return ChainVerificationResult(
                valid=False,
                entries_checked=entry.sequence,
                first_broken_sequence=entry.sequence,
                reason=(
                    f"sequence {entry.sequence}: stored hash {entry.hash!r} does not match "
                    f"recomputed hash {recomputed!r} -- content was altered"
                ),
            )
        expected_previous = entry.hash

    return ChainVerificationResult(valid=True, entries_checked=len(entries))
