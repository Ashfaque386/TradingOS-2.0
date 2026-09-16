import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class KillSwitchState(Base):
    """One row per mode ('live' | 'paper') -- Build Spec §8's requirement
    that the live and paper Max Drawdown Kill Switches are separate
    instances that never share state is enforced here by giving each mode
    its own row behind a UNIQUE constraint, never a shared row keyed any
    other way. `tripped` only ever flips True -> False via an explicit
    `reset_by` (src.orchestration.kill_switch.reset_kill_switch) -- there is
    no code path anywhere in this codebase that sets it False without one.
    """

    __tablename__ = "kill_switch_states"
    __table_args__ = (
        CheckConstraint("mode IN ('live','paper')", name="ck_kill_switch_states_mode"),
        UniqueConstraint("mode", name="uq_kill_switch_states_mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    tripped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trip_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    tripped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    last_drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False, default=15.0)

    reset_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"KillSwitchState(mode={self.mode!r}, tripped={self.tripped!r})"
