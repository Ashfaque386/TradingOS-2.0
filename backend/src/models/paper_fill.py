import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PaperFill(Base):
    """One depth-walked fill (Build Spec §11,
    src.engine.paper_trading.order_book.simulate_fill), possibly one leg
    of a multi-leg order sharing `order_group_id`. `fully_filled=False`
    records an honest partial fill -- there is no retry/rollback path
    anywhere in this codebase that would make a partial fill row
    disappear or "complete itself" later (see
    src.engine.paper_trading.multi_leg's module docstring for the
    multi-leg case specifically).
    """

    __tablename__ = "paper_fills"
    __table_args__ = (CheckConstraint("side IN ('buy','sell')", name="ck_paper_fills_side"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_trading_subscriptions.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)

    # Groups the legs of one multi-leg order; a single-leg order gets its
    # own fresh order_group_id too, so every fill always has one.
    order_group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    leg_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fully_filled: Mapped[bool] = mapped_column(Boolean, nullable=False)

    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"PaperFill(symbol={self.symbol!r}, side={self.side!r}, "
            f"filled_quantity={self.filled_quantity!r}, fully_filled={self.fully_filled!r})"
        )
