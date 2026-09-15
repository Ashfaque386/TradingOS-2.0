"""ApprovalRequest lifecycle (Build Spec §7.3). Modeled generically in this
phase: `subject_type`/`subject_id`/`transition_type` are free-form strings a
caller defines — the concrete Backtesting→PaperTrading transition is wired
in Phase 4, which will pass its own subject_type ("strategy") and
transition_type rather than this phase inventing one.

The unbypassability guarantee lives in src/orchestration/transitions.py's
conditional_transition(): it calls is_transition_approved() (below) itself,
before performing the actual UPDATE, whenever a caller passes an
ApprovalGate. That is the *only* place the check happens — there is no
per-route or per-caller duplicate check to accidentally omit.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.approval_request import ApprovalRequest, ApprovalStatus


async def create_approval_request(
    db: AsyncSession,
    *,
    subject_type: str,
    subject_id: str,
    transition_type: str,
    run_id: uuid.UUID | None = None,
    requested_by: str | None = None,
    reason: str | None = None,
) -> ApprovalRequest:
    request = ApprovalRequest(
        run_id=run_id,
        subject_type=subject_type,
        subject_id=subject_id,
        transition_type=transition_type,
        status=ApprovalStatus.PENDING,
        requested_by=requested_by,
        reason=reason,
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)
    return request


async def is_transition_approved(
    db: AsyncSession, *, subject_type: str, subject_id: str, transition_type: str
) -> bool:
    stmt = select(ApprovalRequest.id).where(
        ApprovalRequest.subject_type == subject_type,
        ApprovalRequest.subject_id == subject_id,
        ApprovalRequest.transition_type == transition_type,
        ApprovalRequest.status == ApprovalStatus.APPROVED,
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def decide_approval_request(
    db: AsyncSession,
    request_id: uuid.UUID,
    *,
    approve: bool,
    decided_by: str,
    reason: str | None = None,
) -> ApprovalRequest | None:
    request = await db.get(ApprovalRequest, request_id)
    if request is None or request.status != ApprovalStatus.PENDING:
        return None

    request.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    request.decided_by = decided_by
    request.decided_at = datetime.now(UTC)
    request.decision_reason = reason
    await db.commit()
    await db.refresh(request)
    return request
