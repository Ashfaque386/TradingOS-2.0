"""Conflict detection (Build Spec §7.3): deterministic checks for
structurally conflicting decisions. Built generically in this phase —
concrete conflict types (market-vs-sentiment, risk-vs-deployment) depend
on agents not yet built (Phase 3+); this module only knows that two
OrganizationalDecisions about the *same* subject artefact and decision
type with *different* verdicts is a conflict, regardless of what those
strings mean to whichever agent recorded them.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.organizational_decision import OrganizationalDecision


@dataclass(frozen=True, slots=True)
class ConflictReport:
    subject_artefact_id: uuid.UUID
    decision_type: str
    conflicting_decision_ids: tuple[uuid.UUID, ...]
    verdicts: tuple[str, ...]


async def record_decision(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    decision_type: str,
    verdict: str,
    task_id: uuid.UUID | None = None,
    subject_artefact_id: uuid.UUID | None = None,
    rationale: str | None = None,
) -> OrganizationalDecision:
    decision = OrganizationalDecision(
        run_id=run_id,
        task_id=task_id,
        decision_type=decision_type,
        subject_artefact_id=subject_artefact_id,
        verdict=verdict,
        rationale=rationale,
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)
    return decision


async def detect_conflicts(db: AsyncSession, run_id: uuid.UUID) -> list[ConflictReport]:
    result = await db.execute(
        select(OrganizationalDecision).where(
            OrganizationalDecision.run_id == run_id,
            OrganizationalDecision.subject_artefact_id.is_not(None),
        )
    )
    decisions = list(result.scalars())

    groups: dict[tuple[uuid.UUID, str], list[OrganizationalDecision]] = {}
    for d in decisions:
        key = (d.subject_artefact_id, d.decision_type)
        groups.setdefault(key, []).append(d)

    reports: list[ConflictReport] = []
    for (subject_artefact_id, decision_type), group in groups.items():
        verdicts = {d.verdict for d in group}
        if len(verdicts) > 1:
            reports.append(
                ConflictReport(
                    subject_artefact_id=subject_artefact_id,
                    decision_type=decision_type,
                    conflicting_decision_ids=tuple(d.id for d in group),
                    verdicts=tuple(sorted(verdicts)),
                )
            )
    return reports
