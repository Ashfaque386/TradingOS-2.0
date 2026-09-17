"""Audit log read/export API (Build Spec §19): list, CSV/NDJSON export
filtered by entity/actor, and an on-demand chain-verification trigger.
Read access (list + export) is role-restricted to exactly
SystemAdministrator/ReadOnlyAuditor, per the spec's explicit wording --
neither PortfolioManager nor RiskManager can read the audit trail, even
though both can trigger the mutating actions that populate it. The verify
trigger is SystemAdministrator-only: a genuine divergence finding writes
its own audit entry (see `src.orchestration.audit_scheduler`'s module
docstring), so this is not a pure read despite living under `/audit`.
"""

import csv
import io
import json
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import AuditChainVerifyResponse, AuditLogEntryResponse
from src.audit.archive import verify_archive_chain_divergence
from src.audit.service import verify_db_chain, write_audit_entry
from src.core.config import get_settings
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.audit_log import AuditLog
from src.models.user import User

router = APIRouter(prefix="/audit", tags=["audit"])

_READ_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.READ_ONLY_AUDITOR]

register_policy("GET", "/api/v1/audit/entries", roles=_READ_ROLES)
register_policy("GET", "/api/v1/audit/export", roles=_READ_ROLES)
register_policy("POST", "/api/v1/audit/verify", roles=[Role.SYSTEM_ADMINISTRATOR])


def get_audit_archive_root() -> Path:
    return Path(get_settings().audit_archive_path)


def _entry_response(row: AuditLog) -> AuditLogEntryResponse:
    return AuditLogEntryResponse(
        id=row.id,
        sequence=row.sequence,
        previous_hash=row.previous_hash,
        hash=row.hash,
        actor=row.actor,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        details=row.details,
        correlation_id=row.correlation_id,
        created_at=row.created_at.isoformat(),
    )


def _apply_filters(stmt, *, entity_type: str | None, entity_id: str | None, actor: str | None):
    if entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == entity_id)
    if actor is not None:
        stmt = stmt.where(AuditLog.actor == actor)
    return stmt


@router.get("/entries")
async def list_audit_entries(
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    limit: int = Query(default=100, le=1000, gt=0),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[AuditLogEntryResponse]:
    stmt = select(AuditLog)
    stmt = _apply_filters(stmt, entity_type=entity_type, entity_id=entity_id, actor=actor)
    stmt = stmt.order_by(AuditLog.sequence.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_entry_response(row) for row in rows]


@router.get("/export")
async def export_audit_entries(
    format: Literal["csv", "ndjson"] = "ndjson",
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> Response:
    stmt = select(AuditLog)
    stmt = _apply_filters(stmt, entity_type=entity_type, entity_id=entity_id, actor=actor)
    stmt = stmt.order_by(AuditLog.sequence)
    rows = (await db.execute(stmt)).scalars().all()

    fieldnames = [
        "sequence",
        "previous_hash",
        "hash",
        "actor",
        "action",
        "entity_type",
        "entity_id",
        "details",
        "correlation_id",
        "created_at",
    ]

    if format == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sequence": row.sequence,
                    "previous_hash": row.previous_hash,
                    "hash": row.hash,
                    "actor": row.actor,
                    "action": row.action,
                    "entity_type": row.entity_type,
                    "entity_id": row.entity_id,
                    "details": json.dumps(row.details) if row.details is not None else "",
                    "correlation_id": row.correlation_id,
                    "created_at": row.created_at.isoformat(),
                }
            )
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit_log_export.csv"},
        )

    lines = [
        json.dumps(
            {
                name: (row.created_at.isoformat() if name == "created_at" else getattr(row, name))
                for name in fieldnames
            }
        )
        for row in rows
    ]
    return Response(
        content="\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=audit_log_export.ndjson"},
    )


@router.post("/verify")
async def verify_audit_chain(
    db: AsyncSession = Depends(get_db),
    archive_root: Path = Depends(get_audit_archive_root),
    current_user: User = Depends(require_role),
) -> AuditChainVerifyResponse:
    db_chain_result = await verify_db_chain(db)
    divergence = await verify_archive_chain_divergence(db, archive_root)

    if divergence.diverged or not db_chain_result.valid:
        await write_audit_entry(
            db,
            actor=current_user.email,
            action="audit.manual_verify_found_divergence",
            entity_type="audit_log",
            entity_id=str(
                divergence.first_diverged_sequence or db_chain_result.first_broken_sequence
            ),
            details={
                "db_chain_valid": db_chain_result.valid,
                "db_chain_reason": db_chain_result.reason,
                "archive_diverged": divergence.diverged,
                "archive_reason": divergence.reason,
            },
        )
        await db.commit()

    return AuditChainVerifyResponse(
        diverged=divergence.diverged,
        archive_internally_valid=divergence.archive_internally_valid,
        live_db_matches_archive=divergence.live_db_matches_archive,
        first_diverged_sequence=divergence.first_diverged_sequence,
        reason=divergence.reason,
        entries_checked_in_db_chain=db_chain_result.entries_checked,
        db_chain_valid=db_chain_result.valid,
        db_chain_reason=db_chain_result.reason,
    )
