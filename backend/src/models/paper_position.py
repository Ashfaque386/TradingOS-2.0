import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PaperPosition(Base):
    """The current average-cost-basis position for one subscription (Build
    Spec §11) -- a deliberate simplification vs FIFO lot matching, see
    src.engine.paper_trading.position_ledger's module docstring. `quantity`
    is signed: positive is long, negative is short. One row per
    subscription (a subscription is already scoped to a single symbol).
    """

    __tablename__ = "paper_positions"
    __table_args__ = (
        UniqueConstraint("subscription_id", name="uq_paper_positions_subscription_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_trading_subscriptions.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"PaperPosition(symbol={self.symbol!r}, quantity={self.quantity!r})"
