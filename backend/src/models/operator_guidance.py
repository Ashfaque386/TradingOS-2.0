import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class OperatorGuidance(Base):
    """Free-text operator guidance (Phase 19, docs/phase19-audit.md Part
    2.6/3.1): the mechanism the audit found genuinely missing -- Phase
    12's in-app chat is reactive Q&A, architecturally isolated from
    `src.orchestration.planner.create_plan`. Every active
    (`is_active=True`) row is folded into the objective text
    `create_plan` passes to the pipeline's `ceo_kickoff` node (see
    `planner.py`'s own docstring for the exact call site), so this is a
    real input to the next planning cycle, not a note nobody reads.
    Deactivated rather than deleted when an operator retires a note, so
    the audit trail (`operator_guidance.created`/`.deactivated`, written
    via `write_audit_entry` at the same call sites) stays accurate.
    """

    __tablename__ = "operator_guidance"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"OperatorGuidance(id={self.id!r}, is_active={self.is_active!r})"
