import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class OrganizationalEvent(Base):
    """One row per src/orchestration/events.py `emit()` call. `sequence` is
    gap-free per `run_id` (enforced by emit()'s advisory-lock-serialized
    "next sequence" computation, not just a DB identity column) — the
    ordering contract the Mission Control activity feed and any future
    Redis-subscriber UI can rely on.
    """

    __tablename__ = "organizational_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"OrganizationalEvent(run_id={self.run_id!r}, sequence={self.sequence!r}, "
            f"event_type={self.event_type!r})"
        )
