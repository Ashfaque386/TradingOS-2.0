import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class DailySignal(Base):
    """One Layer 1 (Build Spec §11) BUY/SELL decision, detected from the
    trusted backtest engine's own position-transition rule
    (src.engine.paper_trading.daily_signal.detect_todays_signal) on the
    most recent daily bar. `consumed` is set once Layer 2 has acted on it
    (entered or exited a position) -- an unconsumed BUY signal is what
    lets a stopped-out position re-enter later the same day without
    waiting for tomorrow's daily run.
    """

    __tablename__ = "daily_signals"
    __table_args__ = (
        CheckConstraint("signal_type IN ('BUY','SELL')", name="ck_daily_signals_signal_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("paper_trading_subscriptions.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    # The daily bar's own close -- the reference_price the per-tick order
    # intent risk gate (src.orchestration.risk_gate) compares proposed
    # fill prices against.
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)

    consumed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"DailySignal(symbol={self.symbol!r}, signal_type={self.signal_type!r})"
