import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiveBatchAuthorization(Base):
    """A human's bounded, in-advance pre-authorization to reduce click-
    fatigue during an active session (Build Spec §12.2): "approve up to
    `max_intents` intents for this strategy between `window_start` and
    `window_end`, each capped at `max_notional_per_intent`." This row IS
    the logged human action -- `authorized_by` is required and never
    defaulted, same posture as src.orchestration.kill_switch.
    reset_kill_switch's `reset_by`.

    `intents_used` is the server's own running count, incremented only by
    src.orchestration.live_trading's own enforcement code inside the same
    transaction that consumes a slot -- never trusted from, or settable
    by, an API caller. That plus the two CheckConstraints below is what
    "enforced server-side, not just suggested in the UI" (Build Spec
    §12.2) means concretely: even a client that already knows this row's
    id cannot make it authorize more than what it was created with.
    """

    __tablename__ = "live_batch_authorizations"
    __table_args__ = (
        CheckConstraint("max_intents > 0", name="ck_live_batch_authorizations_max_intents"),
        CheckConstraint(
            "max_notional_per_intent > 0", name="ck_live_batch_authorizations_max_notional"
        ),
        CheckConstraint(
            "intents_used >= 0 AND intents_used <= max_intents",
            name="ck_live_batch_authorizations_intents_used",
        ),
        CheckConstraint(
            "window_end > window_start", name="ck_live_batch_authorizations_window_order"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    authorized_by: Mapped[str] = mapped_column(String(128), nullable=False)

    max_intents: Mapped[int] = mapped_column(Integer, nullable=False)
    max_notional_per_intent: Mapped[float] = mapped_column(Float, nullable=False)
    intents_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"LiveBatchAuthorization(strategy_id={self.strategy_id!r}, "
            f"max_intents={self.max_intents!r}, intents_used={self.intents_used!r})"
        )
