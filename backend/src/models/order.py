import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Order(Base):
    """A live order actually submitted to a broker adapter (Build Spec
    §12.1: "Order/Trade rows written" on approval). One row per
    `adapter.place_order()` call this codebase makes -- `broker_order_id`
    is the broker's own identifier, nullable only for the `status=failed`
    case where the call never got far enough to receive one (a risk-gate
    rejection re-checked at approval time, a circuit-open broker, a
    network failure).
    """

    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_orders_side"),
        CheckConstraint("status IN ('submitted','failed')", name="ck_orders_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    live_order_intent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("live_order_intents.id"), nullable=False, index=True
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    broker_name: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"Order(symbol={self.symbol!r}, side={self.side!r}, status={self.status!r})"
