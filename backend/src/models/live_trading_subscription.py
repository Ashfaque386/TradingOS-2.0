import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class LiveTradingSubscription(Base):
    """One Strategy enrolled in the human-gated LiveExecutionPipeline
    (Build Spec §12) against a single symbol/broker, using the same
    deterministic builtin signal generators as Phase 7's paper engine
    (src.engine.backtest.signals). Keyed by `strategy_id` (not
    `strategy_version_id`, unlike PaperTradingSubscription) because
    live-eligibility (Strategy.status == LiveEligible/Live) is itself a
    Strategy-level, not version-level, property -- and `live_order_intents`
    (Build Spec §12.3) is likewise keyed by `strategy_id`.

    `last_signal_type`/`last_signal_reference_price`/
    `last_signal_generated_at` hold Layer 1's daily-signal state directly
    on this row rather than in a separate table the way Phase 7's
    `DailySignal` does -- a deliberate simplification for live trading:
    only the *latest* signal ever matters for intent generation (Phase
    7's per-signal audit trail isn't needed here, since every live intent
    this state produces already gets its own persisted, human-visible
    `LiveOrderIntent` row).
    """

    __tablename__ = "live_trading_subscriptions"
    __table_args__ = (
        CheckConstraint(
            "builtin_strategy IN ('always_long','sma_crossover')",
            name="ck_live_trading_subscriptions_builtin_strategy",
        ),
        CheckConstraint(
            "last_signal_type IS NULL OR last_signal_type IN ('BUY','SELL')",
            name="ck_live_trading_subscriptions_last_signal_type",
        ),
        CheckConstraint(
            "intent_expiry_seconds > 0 AND intent_expiry_seconds <= 300",
            name="ck_live_trading_subscriptions_intent_expiry_seconds",
        ),
        # One subscription per (strategy, symbol): _get_live_subscription
        # (src.orchestration.live_trading) looks one up by this exact pair
        # and assumes at most one match.
        UniqueConstraint(
            "strategy_id", "symbol", name="uq_live_trading_subscriptions_strategy_symbol"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    broker_name: Mapped[str] = mapped_column(String(32), nullable=False)

    builtin_strategy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="sma_crossover"
    )
    sma_window: Mapped[int] = mapped_column(Integer, nullable=False, default=15)

    initial_capital: Mapped[float] = mapped_column(Float, nullable=False, default=100_000.0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    position_size_pct: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)

    # Build Spec §12.2: configurable per strategy, hard platform-wide
    # maximum of 300 seconds (5 minutes) -- enforced both here (DB-level
    # CheckConstraint) and in src.orchestration.live_trading at enrollment
    # time, the same two-layers-of-enforcement posture used throughout
    # this codebase's status/constraint columns.
    intent_expiry_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=90)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    last_signal_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    last_signal_reference_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_signal_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The Redis Stream cursor (src.engine.paper_trading.tick_feed --
    # shared with the paper engine's own tick stream per symbol, since a
    # symbol's live quote is the same value regardless of paper/live mode)
    # the intent-generation job resumes reading this symbol's ticks from.
    tick_cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="0")

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"LiveTradingSubscription(symbol={self.symbol!r}, "
            f"strategy_id={self.strategy_id!r}, broker_name={self.broker_name!r})"
        )
