"""Generic ApprovalRequest decision route (Build Spec §7.3's approvals
mechanism, Phase 2) -- deciding an approval is the same action regardless
of what subject_type it's gating (a strategy promotion today, some other
gated transition later), so this lives on its own rather than being
duplicated per subject type.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import ApprovalRequestResponse, DecideApprovalRequest
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.user import User
from src.orchestration.approvals import decide_approval_request

router = APIRouter(prefix="/approvals", tags=["approvals"])

# Deciding who may sign off is a risk-governance action -- SystemAdministrator
# retained for operational/break-glass reasons, matching the rest of this
# codebase's RBAC conventions (e.g. orchestration's pause/continue/retry).
_DECIDE_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("POST", "/api/v1/approvals/{approval_id}/decide", roles=_DECIDE_ROLES)


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
    return ApprovalRequestResponse.model_validate(decided)
