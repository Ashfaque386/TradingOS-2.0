"""Generic ApprovalRequest decision route (Build Spec §7.3's approvals
mechanism, Phase 2) -- deciding an approval is the same action regardless
of what subject_type it's gating (a strategy promotion today, some other
gated transition later), so this lives on its own rather than being
duplicated per subject type.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import ApprovalRequestResponse, DecideApprovalRequest
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.approval_request import ApprovalRequest, ApprovalStatus
from src.models.strategy import Strategy
from src.models.user import User
from src.orchestration.approvals import decide_approval_request

router = APIRouter(prefix="/approvals", tags=["approvals"])

# Deciding who may sign off is a risk-governance action -- SystemAdministrator
# retained for operational/break-glass reasons, matching the rest of this
# codebase's RBAC conventions (e.g. orchestration's pause/continue/retry).
_DECIDE_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("GET", "/api/v1/approvals", roles=list(Role))
register_policy("POST", "/api/v1/approvals/{approval_id}/decide", roles=_DECIDE_ROLES)


async def _labelled(
    db: AsyncSession, approvals: list[ApprovalRequest]
) -> list[ApprovalRequestResponse]:
    """The approvals with the strategy named and the people by e-mail, so a reader sees "RELIANCE
    momentum, asked for by risk@..." and not a pair of ids."""
    strategy_ids: set[uuid.UUID] = set()
    user_ids: set[uuid.UUID] = set()
    for approval in approvals:
        if approval.subject_type == "strategy":
            try:
                strategy_ids.add(uuid.UUID(approval.subject_id))
            except ValueError:
                pass
        for raw in (approval.requested_by, approval.decided_by):
            try:
                user_ids.add(uuid.UUID(raw)) if raw else None
            except ValueError:
                pass
    names: dict[str, str] = {}
    if strategy_ids:
        rows = await db.execute(
            select(Strategy.id, Strategy.name).where(Strategy.id.in_(strategy_ids))
        )
        names = {str(i): n for i, n in rows.all()}
    emails: dict[str, str] = {}
    if user_ids:
        rows = await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
        emails = {str(i): e for i, e in rows.all()}
    labelled = []
    for approval in approvals:
        response = ApprovalRequestResponse.model_validate(approval)
        response.subject_label = names.get(approval.subject_id)
        response.requested_by_label = emails.get(approval.requested_by or "")
        response.decided_by_label = emails.get(approval.decided_by or "")
        labelled.append(response)
    return labelled


@router.get("")
async def list_approvals_endpoint(
    status_filter: ApprovalStatus | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[ApprovalRequestResponse]:
    query = select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())
    if status_filter is not None:
        query = query.where(ApprovalRequest.status == status_filter)
    result = await db.execute(query)
    return await _labelled(db, list(result.scalars().all()))


@router.post("/{approval_id}/decide")
async def decide_approval_endpoint(
    approval_id: uuid.UUID,
    body: DecideApprovalRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> ApprovalRequestResponse:
    decided = await decide_approval_request(
        db,
        approval_id,
        approve=body.approve,
        decided_by=str(current_user.id),
        reason=body.reason,
    )
    if decided is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Approval request not found or already decided",
        )
    return (await _labelled(db, [decided]))[0]
