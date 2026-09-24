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
    fills and Phase 8's Shadow Mode confidence split: `price` is always
    the reference/proposed price the intent was generated and approved
    against, not a broker-confirmed fill price -- even after
    reconciliation, `price` is left untouched and the confirmed figure
    goes in `fill_price` instead, so a caller can never confuse the two.

    `status` starts at `pending_confirmation` (recorded at submission) and
    is later advanced by `src.orchestration.live_trading.reconcile_pending_trades`
    (the scheduled broker-fill reconciliation job, Phase 16 audit
    follow-up B) to `filled`/`rejected`/`cancelled` once the broker's own
    order book reports a terminal outcome for the matching
    `Order.broker_order_id`, or left at `failed` for the pre-existing
    submission-time-failure case that never reaches a broker at all. A
    caller reading `pending_confirmation` must still not mistake it for a
    guarantee of execution -- it only means reconciliation hasn't yet
    observed a terminal broker status for this trade.
    """

    __tablename__ = "trades"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_trades_side"),
        CheckConstraint(
            "status IN ('pending_confirmation','filled','rejected','cancelled','failed')",
            name="ck_trades_status",
        ),
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

    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"Trade(symbol={self.symbol!r}, side={self.side!r}, status={self.status!r})"
