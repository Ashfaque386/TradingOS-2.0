"""The one primitive every status transition in the orchestration engine
goes through (Build Spec §7.3 approvals: "enforced at the status-transition
function itself, not only at one API route"). Race-safety comes from a
conditional `UPDATE ... WHERE id = :id AND <status column> = :from_status`:
if another writer already moved the row away from `from_status`, this
transition affects zero rows and reports False instead of corrupting state.

Approval-gating is optional per call (`approval=` tuple) — a caller
requiring human sign-off for a particular subject/transition passes the
gate; a caller with no such requirement passes none. What makes the gate
unbypassable is that it's checked *inside* this function, not duplicated as
an `if` at the top of each higher-level caller — any code path that
performs the transition through here (whichever function it's called from)
gets the same check for free, and no code path can skip it just by calling
a different-looking function, since there's only one primitive.
"""

from dataclasses import dataclass

from sqlalchemy import Column, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement


class ApprovalRequiredError(Exception):
    """Raised when a gated transition is attempted without a matching
    approved ApprovalRequest."""

    def __init__(self, subject_type: str, subject_id: str, transition_type: str):
        self.subject_type = subject_type
        self.subject_id = subject_id
        self.transition_type = transition_type
        super().__init__(
            f"transition {transition_type!r} on {subject_type}:{subject_id} "
            "requires an approved ApprovalRequest"
        )


@dataclass(frozen=True, slots=True)
class ApprovalGate:
    subject_type: str
    subject_id: str
    transition_type: str


async def conditional_transition(
    db: AsyncSession,
    *,
    table,
    id_column: Column,
    row_id,
    status_column: Column,
    from_status,
    to_status,
    extra_values: dict | None = None,
    approval: ApprovalGate | None = None,
) -> bool:
    """Race-safe UPDATE ... WHERE id = row_id AND status = from_status,
    optionally gated by an already-approved ApprovalRequest. Returns True
    iff the transition was applied; False means the row wasn't in
    from_status anymore (lost a race, or doesn't exist) — never raises for
    that case, only ApprovalRequiredError for a missing/unapproved gate.
    """
    if approval is not None:
        # Imported here, not at module scope, to avoid a circular import
        # (approvals.py depends on nothing from this module, but importing
        # it eagerly at module load time would couple transitions.py's
        # import order to approvals.py's).
        from src.orchestration.approvals import is_transition_approved

        approved = await is_transition_approved(
            db,
            subject_type=approval.subject_type,
            subject_id=approval.subject_id,
            transition_type=approval.transition_type,
        )
        if not approved:
            raise ApprovalRequiredError(
                approval.subject_type, approval.subject_id, approval.transition_type
            )

    values: dict[ColumnElement | str, object] = {status_column: to_status}
    if extra_values:
        values.update(extra_values)

    # .values(values) (a single dict positional arg), not .values(**values):
    # the dict's keys can be Column objects, which aren't valid **kwargs keys.
    stmt = update(table).where(id_column == row_id, status_column == from_status).values(values)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount == 1
