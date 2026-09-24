import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LivePosition(Base):
    """The current approximate position for one live-trading strategy
    (Build Spec §12) -- same average-cost-basis simplification as
    `PaperPosition` (src.engine.paper_trading.position_ledger), reused
    directly rather than reimplemented, with one further honest caveat on
    top: it is updated at order-submission time using the intent's
    reference price (see `Trade`'s docstring for why a confirmed fill
    price isn't available yet), so it tracks "what we've told the broker
    we want," not a broker-reconciled position. `quantity` is signed:
    positive is long, negative is short. One row per strategy (a
    LiveTradingSubscription is already scoped to a single symbol).
    """

    __tablename__ = "live_positions"
    __table_args__ = (UniqueConstraint("strategy_id", name="uq_live_positions_strategy_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"LivePosition(symbol={self.symbol!r}, quantity={self.quantity!r})"
