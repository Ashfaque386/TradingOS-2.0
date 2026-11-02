"""Audit log read/export API (Build Spec §19): list, CSV/NDJSON export
filtered by entity/actor/action/text/time, and an on-demand chain-verification trigger.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.paging import set_total_count
from src.api.schemas import (
    AuditActorSummaryResponse,
    AuditChainVerifyResponse,
    AuditFacetsResponse,
    AuditLogEntryResponse,
    AuditTradeTraceResponse,
)
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
register_policy("GET", "/api/v1/audit/facets", roles=_READ_ROLES)
register_policy("GET", "/api/v1/audit/export", roles=_READ_ROLES)
register_policy("POST", "/api/v1/audit/verify", roles=[Role.SYSTEM_ADMINISTRATOR])
register_policy("GET", "/api/v1/audit/trades/{entity_id}/trace", roles=_READ_ROLES)
register_policy("GET", "/api/v1/audit/actors/{actor}/summary", roles=_READ_ROLES)


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


# The most distinct values of one column that /facets lists. The console offers them as choices in a
# filter; past this many it says the list is cut, and the person types the value instead.
_FACET_LIMIT = 500


@dataclass(frozen=True)
class AuditFilter:
    """What narrows the log. Every field is optional and they combine with AND; `search` is a
    case-insensitive "contains" over actor, action and entity id."""

    entity_type: str | None = None
    entity_id: str | None = None
    actor: str | None = None
    action: str | None = None
    search: str | None = None
    since: datetime | None = None
    until: datetime | None = None


def _as_utc(moment: datetime | None) -> datetime | None:
    """A time given without a zone is read as UTC, the zone the log is stored in."""
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def audit_filter(
    entity_type: str | None = Query(default=None, max_length=64),
    entity_id: str | None = Query(default=None, max_length=128),
    actor: str | None = Query(default=None, max_length=64),
    action: str | None = Query(default=None, max_length=128),
    search: str | None = Query(default=None, max_length=128),
    since: datetime | None = None,
    until: datetime | None = None,
) -> AuditFilter:
    return AuditFilter(
        entity_type=entity_type,
        entity_id=entity_id,
        actor=actor,
        action=action,
        search=search or None,
        since=_as_utc(since),
        until=_as_utc(until),
    )


def _apply_filters(stmt: Select[Any], filters: AuditFilter) -> Select[Any]:
    if filters.entity_type is not None:
        stmt = stmt.where(AuditLog.entity_type == filters.entity_type)
    if filters.entity_id is not None:
        stmt = stmt.where(AuditLog.entity_id == filters.entity_id)
    if filters.actor is not None:
        stmt = stmt.where(AuditLog.actor == filters.actor)
    if filters.action is not None:
        stmt = stmt.where(AuditLog.action == filters.action)
    if filters.search is not None:
        # autoescape: a "%" or "_" typed into the search box is looked for, not read as a wildcard
        contains: list[ColumnElement[bool]] = [
            AuditLog.actor.icontains(filters.search, autoescape=True),
            AuditLog.action.icontains(filters.search, autoescape=True),
            AuditLog.entity_id.icontains(filters.search, autoescape=True),
        ]
        stmt = stmt.where(or_(*contains))
    if filters.since is not None:
        stmt = stmt.where(AuditLog.created_at >= filters.since)
    if filters.until is not None:
        stmt = stmt.where(AuditLog.created_at <= filters.until)
    return stmt


@router.get("/entries")
async def list_audit_entries(
    response: Response,
    filters: AuditFilter = Depends(audit_filter),
    limit: int = Query(default=100, le=1000, gt=0),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> list[AuditLogEntryResponse]:
    """One page, newest first. `X-Total-Count` is how many entries the filters match in all, so a
    person can page through all of them and not only the newest `limit`."""
    total = (
        await db.execute(_apply_filters(select(func.count(AuditLog.id)), filters))
    ).scalar_one()
    set_total_count(response, total)
    stmt = _apply_filters(select(AuditLog), filters)
    stmt = stmt.order_by(AuditLog.sequence.desc()).offset(offset).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [_entry_response(row) for row in rows]


async def _distinct_values(db: AsyncSession, column: Any) -> tuple[list[str], bool]:
    stmt = (
        select(column)
        .where(column.is_not(None))
        .distinct()
        .order_by(column)
        .limit(_FACET_LIMIT + 1)
    )
    values = list((await db.execute(stmt)).scalars().all())
    return values[:_FACET_LIMIT], len(values) > _FACET_LIMIT


@router.get("/facets")
async def get_audit_facets(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> AuditFacetsResponse:
    """The values the log's filters can be set to, for the console's drop-downs. Read once when the
    page opens, not polled: it reads the whole table."""
    actors, actors_cut = await _distinct_values(db, AuditLog.actor)
    entity_types, entity_types_cut = await _distinct_values(db, AuditLog.entity_type)
    actions, actions_cut = await _distinct_values(db, AuditLog.action)
    return AuditFacetsResponse(
        actors=actors,
        entity_types=entity_types,
        actions=actions,
        truncated=actors_cut or entity_types_cut or actions_cut,
    )


@router.get("/trades/{entity_id}/trace")
async def get_trade_audit_trace(
    entity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> AuditTradeTraceResponse:
    """The same rows `GET /entries?entity_id=...` already returns, shaped
    as "everything that happened to this one thing" rather than a generic
    filtered list -- every real mutation this codebase writes is tagged
    with the id of the row it mutated (order, trade, live order intent,
    paper fill, ...), so this works for any of them despite the
    trade-shaped path name."""
    stmt = select(AuditLog).where(AuditLog.entity_id == entity_id).order_by(AuditLog.sequence)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No audit entries found for this entity_id")
    return AuditTradeTraceResponse(
        entity_id=entity_id,
        entry_count=len(rows),
        entries=[_entry_response(row) for row in rows],
    )


@router.get("/actors/{actor}/summary")
async def get_actor_audit_summary(
    actor: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> AuditActorSummaryResponse:
    """A rollup over the same rows `GET /entries?actor=...` already
    returns -- action counts plus first/last-seen, computed in the
    database rather than pulled client-side, since an active actor can
    have thousands of rows."""
    action_counts_stmt = (
        select(AuditLog.action, func.count())
        .where(AuditLog.actor == actor)
        .group_by(AuditLog.action)
        .order_by(func.count().desc())
    )
    action_rows = (await db.execute(action_counts_stmt)).all()
    if not action_rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No audit entries found for this actor")

    span_stmt = select(func.min(AuditLog.created_at), func.max(AuditLog.created_at)).where(
        AuditLog.actor == actor
    )
    first_seen, last_seen = (await db.execute(span_stmt)).one()

    return AuditActorSummaryResponse(
        actor=actor,
        action_count=sum(count for _, count in action_rows),
        first_seen=first_seen.isoformat() if first_seen else None,
        last_seen=last_seen.isoformat() if last_seen else None,
        actions={action: count for action, count in action_rows},
    )


@router.get("/export")
async def export_audit_entries(
    format: Literal["csv", "ndjson"] = "ndjson",
    filters: AuditFilter = Depends(audit_filter),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> Response:
    """The same filters as the list, so "export what I am looking at" is what comes back."""
    stmt = _apply_filters(select(AuditLog), filters)
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
