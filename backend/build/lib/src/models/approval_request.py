import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalRequest(Base, TimestampMixin):
    """Blocks a defined transition until a human decides (Build Spec §7.3).
    Modeled generically in this phase: `transition_type` is a caller-defined
    free-form label, not yet a fixed enum — the concrete
    Backtesting→PaperTrading transition (strategies.status) is wired in
    Phase 4, which is expected to pass its own `transition_type` string
    here rather than this phase inventing one prematurely.

    Enforcement lives in src/orchestration/transitions.py's
    conditional_transition(), not in any one API route or caller function —
    see src/orchestration/approvals.py for the unbypassability contract.
    """

    __tablename__ = "approval_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=True, index=True
    )
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(128), nullable=False)
    transition_type: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(
            ApprovalStatus,
            name="approval_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        default=ApprovalStatus.PENDING,
    )
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"ApprovalRequest(id={self.id!r}, subject_type={self.subject_type!r}, "
            f"status={self.status!r})"
        )
