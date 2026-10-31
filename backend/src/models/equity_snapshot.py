import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class EquitySnapshot(Base):
    """One reading of a mode's equity (src.orchestration.equity): what the account was worth at
    `taken_at`, built from the capital it started with, what it has realized net of costs, and
    what its open positions are worth at the latest price.

    `equity` is NULL, never zero, when it could not be measured -- an open position with no
    price, or (live) no starting capital configured -- and `note` says why. The drawdown
    monitor does not evaluate a NULL reading, so a gap in the data can never look like a loss
    (or like a recovery). `peak_equity` and `drawdown_pct` are the figures the monitor used for
    that reading, kept so the chart and the kill switch's trip reason always agree.
    """

    __tablename__ = "equity_snapshots"
    __table_args__ = (
        CheckConstraint("mode IN ('live','paper')", name="ck_equity_snapshots_mode"),
        Index("ix_equity_snapshots_mode_taken_at", "mode", "taken_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    capital_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized: Mapped[float | None] = mapped_column(Float, nullable=True)
    open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unpriced_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 'real' | 'synthetic' | 'none': where the prices came from (the unrealized endpoint's word).
    price_source: Mapped[str] = mapped_column(String(16), nullable=False, default="none")

    peak_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"EquitySnapshot(mode={self.mode!r}, taken_at={self.taken_at!r})"
