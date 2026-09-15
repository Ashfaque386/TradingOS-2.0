import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class OrganizationalDecision(Base):
    """A verdict some task/agent reached about a subject artefact. Built
    generically in this phase (Build Spec §7.3): concrete conflict types
    (market-vs-sentiment, risk-vs-deployment) depend on agents not yet
    built (Phase 3+) — src/orchestration/decisions.py's conflict detection
    only relies on `subject_artefact_id` + `decision_type` grouping and
    `verdict` equality, not on any agent-specific meaning of those strings.
    """

    __tablename__ = "organizational_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    decision_type: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_artefact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("result_artefacts.id"), nullable=True, index=True
    )
    verdict: Mapped[str] = mapped_column(String(128), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"OrganizationalDecision(id={self.id!r}, decision_type={self.decision_type!r}, "
            f"verdict={self.verdict!r})"
        )
