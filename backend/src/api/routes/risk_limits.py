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

from src.api.schemas import RiskLimitChangeResponse, RiskLimitResponse, StageRiskLimitChangeRequest
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.risk_limit import RiskLimit, RiskLimitChangeRequest
from src.models.user import User
from src.orchestration.risk_limits import (
    NoSuchRiskLimitChangeError,
    RiskLimitChangeNotConfirmedError,
    RiskLimitChangeNotStagedError,
    SelfConfirmationNotAllowedError,
    apply_risk_limit_change,
    confirm_risk_limit_change,
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
register_policy("GET", "/api/v1/risk-limits", roles=list(Role))


def _to_response(change: RiskLimitChangeRequest) -> RiskLimitChangeResponse:
    return RiskLimitChangeResponse(
        id=change.id,
        limit_name=change.limit_name,
        proposed_value=change.proposed_value,
        status=change.status,
        staged_by=change.staged_by,
        confirmed_by=change.confirmed_by,
        applied_by=change.applied_by,
    )


@router.post("/stage", status_code=status.HTTP_201_CREATED)
async def stage_risk_limit_change_endpoint(
    body: StageRiskLimitChangeRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> RiskLimitChangeResponse:
    change = await stage_risk_limit_change(
        db,
        limit_name=body.limit_name,
        proposed_value=body.proposed_value,
        reason=body.reason,
        staged_by=str(current_user.id),
    )
    return _to_response(change)


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
    return _to_response(change)


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
    return _to_response(change)


@router.get("")
async def list_risk_limits_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[RiskLimitResponse]:
    result = await db.execute(select(RiskLimit))
    return [RiskLimitResponse(name=row.name, value=row.value) for row in result.scalars().all()]
