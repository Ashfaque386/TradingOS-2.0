import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiveBatchAuthorization(Base):
    """**Retired by Phase 18, table kept only for historical rows.** This
    was a human's bounded, in-advance pre-authorization to reduce click-
    fatigue during an active human-approval session (the original Build
    Spec §12.2 design: "approve up to `max_intents` intents for this
    strategy between `window_start` and `window_end`"). Phase 18 removed
    the per-order human-approval gate entirely and replaced it with a
    standing, always-on cap directly on `LiveTradingSubscription`
    (`max_intents_per_window`/`rate_limit_window_minutes`/
    `max_notional_per_intent`) that every autonomous order is checked
    against unconditionally -- there is no more "batch" of approvals to
    pre-authorize. No code anywhere in this codebase creates, reads, or
    writes a row here anymore (`_find_eligible_batch_authorization` and
    `create_batch_authorization` were deleted in the same change); this
    model and its table stay only because migrations here are additive-
    only (Non-Negotiable Rule #8) and `live_order_intents.
    batch_authorization_id` still has a live FK to it for old rows.
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
