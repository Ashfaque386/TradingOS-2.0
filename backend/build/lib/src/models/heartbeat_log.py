import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class HeartbeatStatus(StrEnum):
    OK = "ok"
    ALERT = "alert"


class HeartbeatLog(Base):
    """One row per heartbeat self-check (Build Spec §17) — read-only by
    construction: src/agents/heartbeat.py, which writes these rows, never
    imports src.execution or src.risk (see that module's docstring), so
    nothing that produces a HeartbeatLog can also be what placed an order
    or mutated a risk limit.
    """

    __tablename__ = "heartbeat_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_identities.agent_id"), nullable=False, index=True
    )
    status: Mapped[HeartbeatStatus] = mapped_column(
        Enum(
            HeartbeatStatus,
            name="heartbeat_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"HeartbeatLog(agent_id={self.agent_id!r}, status={self.status!r})"
