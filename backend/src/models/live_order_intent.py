import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiveOrderIntent(Base):
    """Build Spec §12.3, verbatim column set: `id`, `strategy_id`,
    `symbol`, `side`, `quantity`, `intent_type` (`entry`/`exit`/`stop`),
    `generated_at`, `expires_at`, `status`
    (`pending_approval`/`approved`/`rejected`/`expired`/`submitted`/
    `failed`), `approved_by`, `approved_at`, `resulting_order_id`,
    `batch_authorization_id` (nullable, for the pre-authorized batch
    flow).

    This table is the whole point of Build Spec §12: every row starts
    `pending_approval` and NOTHING in this codebase writes `submitted`
    directly from generation -- see src.orchestration.live_trading's
    module docstring for the full state machine and exactly which
    functions may perform which transition. `status='failed'` covers both
    a risk-gate rejection re-checked at approval time and a broker-level
    failure at submission time; either way, no order reached the broker
    in a way that could place real capital at risk beyond what the human
    approver explicitly authorized.
    """

    __tablename__ = "live_order_intents"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_live_order_intents_side"),
        CheckConstraint(
            "intent_type IN ('entry','exit','stop')", name="ck_live_order_intents_intent_type"
        ),
        CheckConstraint(
            "status IN ('pending_approval','approved','rejected','expired',"
            "'submitted','failed')",
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
