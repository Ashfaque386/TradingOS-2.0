"""Operator Guidance (Phase 19, docs/phase19-audit.md Part 2.6/3.1): the
mechanism the audit found genuinely missing. Folded into
`src.orchestration.run_control.create_run`'s objective text before
`create_plan` runs, so every active row is a real input to the next
planning cycle Mission Control's "give the organization a new objective"
box triggers -- not a note nobody reads.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.audit.service import write_audit_entry
from src.models.operator_guidance import OperatorGuidance


class NoSuchGuidanceError(Exception):
    pass


async def create_guidance(db: AsyncSession, *, message: str, created_by: str) -> OperatorGuidance:
    guidance = OperatorGuidance(message=message, created_by=created_by)
    db.add(guidance)
    await db.flush()
    await write_audit_entry(
        db,
        actor=created_by,
        action="operator_guidance.created",
        entity_type="operator_guidance",
        entity_id=str(guidance.id),
        details={"message": message},
    )
    await db.commit()
    await db.refresh(guidance)
    return guidance


async def deactivate_guidance(
    db: AsyncSession, guidance_id: uuid.UUID, *, actor: str
) -> OperatorGuidance:
    guidance = await db.get(OperatorGuidance, guidance_id)
    if guidance is None:
        raise NoSuchGuidanceError(f"no such operator guidance: {guidance_id}")

    if guidance.is_active:
        guidance.is_active = False
        guidance.deactivated_at = datetime.now(UTC)
        await write_audit_entry(
            db,
            actor=actor,
            action="operator_guidance.deactivated",
            entity_type="operator_guidance",
            entity_id=str(guidance.id),
        )
        await db.commit()
        await db.refresh(guidance)
    return guidance


async def list_guidance(db: AsyncSession, *, active_only: bool = False) -> list[OperatorGuidance]:
    query = select(OperatorGuidance).order_by(OperatorGuidance.created_at.desc())
    if active_only:
        query = query.where(OperatorGuidance.is_active.is_(True))
    result = await db.execute(query)
    return list(result.scalars().all())


async def fold_guidance_into_objective(db: AsyncSession, objective: str) -> str:
    """The real wiring: called from `run_control.create_run` before
    `create_plan` runs. Every active guidance row is appended, in
    creation order, to the objective text the planner actually receives
    -- the CEO Agent's next planning cycle genuinely sees it, not just a
    note stored somewhere nobody reads."""
    active = await list_guidance(db, active_only=True)
    if not active:
        return objective

    notes = "\n".join(f"- {g.message}" for g in reversed(active))
    return f"{objective}\n\nStanding operator guidance (read and consider this):\n{notes}"
