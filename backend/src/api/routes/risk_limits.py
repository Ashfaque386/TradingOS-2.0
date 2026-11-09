"""Dual-control risk-limit API (Build Spec §8.5): stage -> confirm (by a
different, sufficiently-privileged user) -> apply. This is the only HTTP
surface that can change a value backing `infra.riskThresholdRefs`
(src.gateway.schema) -- the config file loader itself has no write path for
these values at all.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    RejectRiskLimitChangeRequest,
    RiskLimitCatalogEntry,
    RiskLimitChangeResponse,
    RiskLimitResponse,
    StageRiskLimitChangeRequest,
)
from src.audit.service import write_audit_entry
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.risk_limit import RiskLimit, RiskLimitChangeRequest
from src.models.user import User
from src.orchestration.risk_limit_catalog import RISK_LIMIT_CATALOG, limit_spec
from src.orchestration.risk_limits import (
    NoSuchRiskLimitChangeError,
    RiskLimitChangeNotConfirmedError,
    RiskLimitChangeNotRejectableError,
    RiskLimitChangeNotStagedError,
    SelfConfirmationNotAllowedError,
    apply_risk_limit_change,
    confirm_risk_limit_change,
    reject_risk_limit_change,
    stage_risk_limit_change,
)

router = APIRouter(prefix="/risk-limits", tags=["risk-limits"])

# Staging, confirming, and applying a risk-limit change are all risk-
# governance actions -- same privileged pair as Phase 2's approvals decide
# roles (src/api/routes/approvals.py). RBAC alone doesn't stop self-
# confirmation (two SystemAdministrator accounts, say) -- that guarantee
# lives inside confirm_risk_limit_change itself.
_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("POST", "/api/v1/risk-limits/stage", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/risk-limits/{change_id}/confirm", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/risk-limits/{change_id}/apply", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/risk-limits/{change_id}/reject", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/risk-limits", roles=list(Role))
register_policy("GET", "/api/v1/risk-limits/changes", roles=list(Role))
register_policy("GET", "/api/v1/risk-limits/catalog", roles=list(Role))


async def _labels(db: AsyncSession, changes: list[RiskLimitChangeRequest]) -> dict[str, str]:
    """user id -> e-mail for everyone named on these changes (an unknown id stays unlabelled)."""
    ids: set[uuid.UUID] = set()
    for change in changes:
        for raw in (change.staged_by, change.confirmed_by, change.applied_by, change.rejected_by):
            if not raw:
                continue
            try:
                ids.add(uuid.UUID(raw))
            except ValueError:
                continue
    if not ids:
        return {}
    rows = await db.execute(select(User.id, User.email).where(User.id.in_(ids)))
    return {str(user_id): email for user_id, email in rows.all()}


def _to_response(
    change: RiskLimitChangeRequest, labels: dict[str, str] | None = None
) -> RiskLimitChangeResponse:
    labels = labels or {}

    def label(raw: str | None) -> str | None:
        return labels.get(raw) if raw else None

    return RiskLimitChangeResponse(
        id=change.id,
        limit_name=change.limit_name,
        proposed_value=change.proposed_value,
        reason=change.reason,
        status=change.status,
        staged_by=change.staged_by,
        staged_at=change.staged_at,
        confirmed_by=change.confirmed_by,
        confirmed_at=change.confirmed_at,
        applied_by=change.applied_by,
        applied_at=change.applied_at,
        rejected_by=change.rejected_by,
        rejected_at=change.rejected_at,
        rejection_reason=change.rejection_reason,
        staged_by_label=label(change.staged_by),
        confirmed_by_label=label(change.confirmed_by),
        applied_by_label=label(change.applied_by),
        rejected_by_label=label(change.rejected_by),
    )


@router.post("/stage", status_code=status.HTTP_201_CREATED)
async def stage_risk_limit_change_endpoint(
    body: StageRiskLimitChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> RiskLimitChangeResponse:
    spec = limit_spec(body.limit_name)
    if spec is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"unknown risk limit {body.limit_name!r} (known: "
            f"{', '.join(s.name for s in RISK_LIMIT_CATALOG)})",
        )
    if not spec.minimum <= body.proposed_value <= spec.maximum:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"{spec.label} must be between {spec.minimum:g} and {spec.maximum:g} {spec.unit}",
        )
    change = await stage_risk_limit_change(
        db,
        limit_name=body.limit_name,
        proposed_value=body.proposed_value,
        reason=body.reason,
        staged_by=str(current_user.id),
    )
    return _to_response(change, await _labels(db, [change]))


@router.post("/{change_id}/confirm")
async def confirm_risk_limit_change_endpoint(
    change_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> RiskLimitChangeResponse:
    try:
        change = await confirm_risk_limit_change(db, change_id, confirmed_by=str(current_user.id))
    except NoSuchRiskLimitChangeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RiskLimitChangeNotStagedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except SelfConfirmationNotAllowedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return _to_response(change, await _labels(db, [change]))


@router.post("/{change_id}/apply")
async def apply_risk_limit_change_endpoint(
    change_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> RiskLimitChangeResponse:
    try:
        change = await apply_risk_limit_change(db, change_id, applied_by=str(current_user.id))
    except NoSuchRiskLimitChangeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RiskLimitChangeNotConfirmedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(change, await _labels(db, [change]))


@router.post("/{change_id}/reject")
async def reject_risk_limit_change_endpoint(
    change_id: uuid.UUID,
    body: RejectRiskLimitChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> RiskLimitChangeResponse:
    reason = (body.reason or "").strip() or None
    try:
        change = await reject_risk_limit_change(
            db, change_id, rejected_by=str(current_user.id), reason=reason
        )
    except NoSuchRiskLimitChangeError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RiskLimitChangeNotRejectableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await write_audit_entry(
        db,
        actor=current_user.email,
        action="risk_limit.change_rejected",
        entity_type="risk-limit",
        entity_id=change.limit_name,
        details={
            "change_id": str(change.id),
            "proposed_value": change.proposed_value,
            "reason": reason,
        },
    )
    await db.commit()
    return _to_response(change, await _labels(db, [change]))


@router.get("/changes")
async def list_risk_limit_changes_endpoint(
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RiskLimitChangeResponse]:
    query = select(RiskLimitChangeRequest).order_by(RiskLimitChangeRequest.staged_at.desc())
    if status_filter is not None:
        query = query.where(RiskLimitChangeRequest.status == status_filter)
    result = await db.execute(query)
    changes = list(result.scalars().all())
    labels = await _labels(db, changes)
    return [_to_response(row, labels) for row in changes]


@router.get("/catalog")
async def risk_limit_catalog_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RiskLimitCatalogEntry]:
    applied = {row.name: row.value for row in (await db.execute(select(RiskLimit))).scalars().all()}
    return [
        RiskLimitCatalogEntry(
            name=spec.name,
            label=spec.label,
            description=spec.description,
            unit=spec.unit,
            default=spec.default,
            minimum=spec.minimum,
            maximum=spec.maximum,
            enforced=spec.enforced,
            effective=applied.get(spec.name, spec.default),
            applied=spec.name in applied,
        )
        for spec in RISK_LIMIT_CATALOG
    ]


@router.get("")
async def list_risk_limits_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RiskLimitResponse]:
    result = await db.execute(select(RiskLimit))
    return [RiskLimitResponse(name=row.name, value=row.value) for row in result.scalars().all()]
