import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiveOrderIntent(Base):
    """Build Spec §12.3's column set, `status` vocabulary redesigned in
    Phase 18: `id`, `strategy_id`, `symbol`, `side`, `quantity`,
    `intent_type` (`entry`/`exit`/`stop`), `generated_at`, `expires_at`,
    `status`, `approved_by`, `approved_at`, `resulting_order_id`,
    `batch_authorization_id`.

    **Phase 18 removed the per-order human-approval gate** -- see
    src.orchestration.live_trading's module docstring for the full
    rationale and the new state machine. A row now starts `generated`
    (not `pending_approval`) and resolves, synchronously in the same call
    that created it, to `submitted` or `failed`; it can also resolve to
    `capped` (the standing rate/notional cap blocked it before it was
    ever attempted) or `expired` (the rare case nothing could submit it
    at generation time -- no broker adapter configured -- and the sweep
    later resolves it). `approved_by`/`approved_at`/`batch_authorization_id`
    are never written by any code path anymore -- no human ever approves
    an intent now, so there is honestly nothing to record there; they
    stay on this table only because migrations here are additive-only
    (Non-Negotiable Rule #8), never because they're still meaningful.

    The CHECK constraint below is deliberately the *union* of the old and
    new vocabularies, not a replacement -- a production database may
    still hold historical rows with `pending_approval`/`approved`/
    `rejected` from before this phase, and Postgres validates a new CHECK
    constraint against every existing row when it's added. New code never
    writes those three values again; they remain valid only so old rows
    don't retroactively violate the schema.
    """

    __tablename__ = "live_order_intents"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_live_order_intents_side"),
        CheckConstraint(
            "intent_type IN ('entry','exit','stop')", name="ck_live_order_intents_intent_type"
        ),
        CheckConstraint(
            "status IN ('pending_approval','approved','rejected','expired',"
            "'submitted','failed','generated','capped')",
            name="ck_live_order_intents_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_type: Mapped[str] = mapped_column(String(8), nullable=False)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_approval")

    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # use_alter=True: this and Order.live_order_intent_id form a genuine
    # circular FK pair (an intent optionally points at the order it
    # produced; an order always points back at the intent that produced
    # it). use_alter tells SQLAlchemy to create/drop this specific FK via
    # a separate ALTER TABLE, which is what actually lets both
    # Base.metadata.create_all/drop_all (used by tests/conftest.py's
    # per-test schema setup) and Alembic resolve the cycle -- without it,
    # both raise CircularDependencyError. The Alembic migration
    # (3363a2fdacf1) does this by hand for the same reason.
    resulting_order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "orders.id", use_alter=True, name="fk_live_order_intents_resulting_order_id_orders"
        ),
        nullable=True,
    )
    batch_authorization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("live_batch_authorizations.id"), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return (
            f"LiveOrderIntent(symbol={self.symbol!r}, side={self.side!r}, "
            f"status={self.status!r}, intent_type={self.intent_type!r})"
        )
