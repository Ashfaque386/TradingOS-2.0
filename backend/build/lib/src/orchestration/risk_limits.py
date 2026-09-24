"""Dual-control risk-limit mutation (Build Spec §8.5): stage -> confirm (by
a *different* user) -> apply. This is now the only way a value backing
`infra.riskThresholdRefs` (src.gateway.schema.RiskThresholdRefs) actually
changes -- the config file only ever points at these values, never sets
them (see that module's docstring).

`confirm_risk_limit_change` is where the self-confirmation guarantee
lives: it compares `confirmed_by` against the stored `staged_by` itself,
so it can't be bypassed by calling some other function -- the same "check
inside the one primitive, not duplicated per caller" posture as
src.orchestration.transitions.conditional_transition.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.risk_limit import RiskLimit, RiskLimitChangeRequest, RiskLimitChangeStatus

__all__ = [
    "NoSuchRiskLimitChangeError",
    "RiskLimitChangeNotConfirmedError",
    "RiskLimitChangeNotStagedError",
    "SelfConfirmationNotAllowedError",
    "apply_risk_limit_change",
    "confirm_risk_limit_change",
    "get_effective_risk_limit",
    "stage_risk_limit_change",
]


class NoSuchRiskLimitChangeError(Exception):
    def __init__(self, change_id: uuid.UUID):
        self.change_id = change_id
        super().__init__(f"no risk limit change request {change_id}")


class RiskLimitChangeNotStagedError(Exception):
    def __init__(self, change_id: uuid.UUID, status: str):
        self.change_id = change_id
        self.status = status
        super().__init__(f"risk limit change {change_id} is {status!r}, not 'staged'")


class RiskLimitChangeNotConfirmedError(Exception):
    def __init__(self, change_id: uuid.UUID, status: str):
        self.change_id = change_id
        self.status = status
        super().__init__(f"risk limit change {change_id} is {status!r}, not 'confirmed'")


class SelfConfirmationNotAllowedError(Exception):
    def __init__(self, change_id: uuid.UUID, actor: str):
        self.change_id = change_id
        self.actor = actor
        super().__init__(f"{actor} staged risk limit change {change_id} and cannot confirm it too")


async def stage_risk_limit_change(
    db: AsyncSession,
    *,
    limit_name: str,
    proposed_value: float,
    staged_by: str,
    reason: str | None = None,
) -> RiskLimitChangeRequest:
    change = RiskLimitChangeRequest(
        limit_name=limit_name,
        proposed_value=proposed_value,
        reason=reason,
        status=RiskLimitChangeStatus.STAGED,
        staged_by=staged_by,
    )
    db.add(change)
    await db.commit()
    await db.refresh(change)
    return change


async def confirm_risk_limit_change(
    db: AsyncSession, change_id: uuid.UUID, *, confirmed_by: str
) -> RiskLimitChangeRequest:
    change = await db.get(RiskLimitChangeRequest, change_id)
    if change is None:
        raise NoSuchRiskLimitChangeError(change_id)
    if change.status != RiskLimitChangeStatus.STAGED:
        raise RiskLimitChangeNotStagedError(change_id, change.status)
    if confirmed_by == change.staged_by:
        raise SelfConfirmationNotAllowedError(change_id, confirmed_by)

    change.status = RiskLimitChangeStatus.CONFIRMED
    change.confirmed_by = confirmed_by
    change.confirmed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(change)
    return change


async def apply_risk_limit_change(
    db: AsyncSession, change_id: uuid.UUID, *, applied_by: str
) -> RiskLimitChangeRequest:
    change = await db.get(RiskLimitChangeRequest, change_id)
    if change is None:
        raise NoSuchRiskLimitChangeError(change_id)
    if change.status != RiskLimitChangeStatus.CONFIRMED:
        raise RiskLimitChangeNotConfirmedError(change_id, change.status)

    result = await db.execute(select(RiskLimit).where(RiskLimit.name == change.limit_name))
    limit = result.scalar_one_or_none()
    if limit is None:
        limit = RiskLimit(
            name=change.limit_name, value=change.proposed_value, updated_by=applied_by
        )
        db.add(limit)
    else:
        limit.value = change.proposed_value
        limit.updated_by = applied_by

    change.status = RiskLimitChangeStatus.APPLIED
    change.applied_by = applied_by
    change.applied_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(change)
    return change


async def get_effective_risk_limit(db: AsyncSession, name: str, default: float) -> float:
    """Reads the current dual-control-applied value for `name`, falling
    back to `default` when no change has ever been applied for it -- never
    raises, so every other risk module keeps working with sane defaults
    before any operator has staged a single change."""
    result = await db.execute(select(RiskLimit.value).where(RiskLimit.name == name))
    value = result.scalar_one_or_none()
    return value if value is not None else default
