import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class AuditLog(Base):
    """Minimal append-only audit trail, added now because the Agent Gateway
    (Phase 1) needs to emit an entry on every config apply/reject.

    This is intentionally NOT the full audit log design from Build Spec
    §19 (hash-chained SHA-256, DB-trigger-enforced immutability,
    advisory-lock-serialized writers, WORM archival) — that's Phase 11.
    Application code never updates or deletes a row here, but nothing at
    the DB level forbids it yet; Phase 11 replaces this table (or adds
    the trigger/hash-chain columns to it) rather than building a second,
    parallel audit mechanism.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"AuditLog(actor={self.actor!r}, action={self.action!r})"
