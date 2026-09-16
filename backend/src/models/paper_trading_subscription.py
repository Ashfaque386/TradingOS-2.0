import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PaperTradingSubscription(Base):
    """One StrategyVersion enrolled in the autonomous paper trading engine
    (Build Spec §11) against a single symbol, using one of Phase 5's
    deterministic builtin signal generators (never arbitrary strategy
    code -- see src.engine.backtest.signals's module docstring for why).
    Layer 1's daily job iterates active subscriptions to generate
    `DailySignal` rows; Layer 2's intraday job iterates them to know which
    symbols' tick streams to drain and what stop-loss to apply.
    """

    __tablename__ = "paper_trading_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "builtin_strategy IN ('always_long','sma_crossover')",
            name="ck_paper_trading_subscriptions_builtin_strategy",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    builtin_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sma_crossover"
    )
    sma_window: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100_000.0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    # Fraction of initial_capital committed to a single entry -- kept well
    # under src.engine.risk.compliance's default 10% position limit so a
    # fresh entry doesn't trip that check by construction; a real position
    # sizer (src.engine.risk.position_sizing, ATR-based) is a documented
    # enhancement point this phase's scope doesn't require wiring in here.
    position_size_pct: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # The Redis Stream cursor (src.engine.paper_trading.tick_feed) the
    # intraday drain job resumes reading this symbol's ticks from -- "0"
    # (the model default) means "never read, start from the beginning."
    tick_cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="0")

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"PaperTradingSubscription(symbol={self.symbol!r}, "
            f"strategy_version_id={self.strategy_version_id!r})"
        )
