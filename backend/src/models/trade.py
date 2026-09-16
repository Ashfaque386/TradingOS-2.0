import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Trade(Base):
    """The trade record accompanying an `Order` (Build Spec §12.1). Real
    broker fill confirmation is asynchronous -- neither Kite Connect's nor
    Upstox's `place_order` response (src.brokers.zerodha/upstox) carries a
    confirmed execution price or quantity, only an order id. This is a
    deliberate honesty line, same posture as Phase 7's honest partial
    fills and Phase 8's Shadow Mode confidence split: `price` here is the
    reference/proposed price the intent was generated and approved
    against, not a broker-confirmed fill price, and `status` is always
    `pending_confirmation` (recorded at submission) or `failed` -- never
    `filled`, since this codebase has no execution-report polling or
    webhook ingestion yet (a Phase 11-class Audit & Observability
    concern). A caller reading this table must not mistake
    `pending_confirmation` for a guarantee the broker actually executed
    the order.
    """

    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_trades_side"),
        CheckConstraint("status IN ('pending_confirmation','failed')", name="ck_trades_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending_confirmation")

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"Trade(symbol={self.symbol!r}, side={self.side!r}, status={self.status!r})"
