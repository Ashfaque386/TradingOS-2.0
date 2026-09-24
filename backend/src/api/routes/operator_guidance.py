"""Operator Guidance API (Phase 19, docs/phase19-audit.md Part 2.6/3.1)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.operator_guidance import OperatorGuidance
from src.models.user import User
from src.orchestration.operator_guidance import (
    NoSuchGuidanceError,
    create_guidance,
    deactivate_guidance,
    list_guidance,
)

router = APIRouter(prefix="/operator-guidance", tags=["operator-guidance"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER, Role.RISK_MANAGER]

register_policy("GET", "/api/v1/operator-guidance", roles=list(Role))
register_policy("POST", "/api/v1/operator-guidance", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/operator-guidance/{guidance_id}/deactivate", roles=_OPERATOR_ROLES)


class CreateGuidanceRequest(BaseModel):
    message: str


class GuidanceResponse(BaseModel):
    id: uuid.UUID
    message: str
    created_by: str
    is_active: bool
    created_at: str
    deactivated_at: str | None


def _response(guidance: OperatorGuidance) -> GuidanceResponse:
    return GuidanceResponse(
        id=guidance.id,
        message=guidance.message,
        created_by=guidance.created_by,
        is_active=guidance.is_active,
        created_at=guidance.created_at.isoformat(),
        deactivated_at=guidance.deactivated_at.isoformat() if guidance.deactivated_at else None,
    )


@router.get("")
async def list_guidance_endpoint(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[GuidanceResponse]:
    rows = await list_guidance(db)
    return [_response(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_guidance_endpoint(
    body: CreateGuidanceRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> GuidanceResponse:
    guidance = await create_guidance(db, message=body.message, created_by=current_user.email)
    return _response(guidance)


@router.post("/{guidance_id}/deactivate")
async def deactivate_guidance_endpoint(
    guidance_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> GuidanceResponse:
    try:
        guidance = await deactivate_guidance(db, guidance_id, actor=current_user.email)
    except NoSuchGuidanceError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _response(guidance)
