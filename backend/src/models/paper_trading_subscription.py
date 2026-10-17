import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
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
        CheckConstraint(
            "instrument_kind IN ('equity','future')",
            name="ck_paper_trading_subscriptions_instrument_kind",
        ),
        CheckConstraint(
            "instrument_kind = 'equity' OR (contract_symbol IS NOT NULL "
            "AND contract_expiry IS NOT NULL AND lot_size IS NOT NULL)",
            name="ck_paper_trading_subscriptions_future_has_contract",
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

    # What is traded. An `equity` subscription trades `symbol` itself. A `future` one reads its
    # signals from `symbol` (the underlying: NIFTY, RELIANCE) and trades `contract_symbol`, the
    # monthly future the roll job keeps pointed at the right expiry, in whole lots of `lot_size`
    # (the broker's figure when the contract was picked, refreshed at each roll).
    instrument_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="equity", server_default="equity"
    )
    contract_symbol: Mapped[str | None] = mapped_column(String(48), nullable=True)
    contract_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    lot_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lots: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Set by the deactivate control, cleared by activate; NULL for a subscription that was
    # deactivated before these existed and has no audit entry saying who.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    deactivation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The Redis Stream cursor (src.engine.paper_trading.tick_feed) the
    # intraday drain job resumes reading this symbol's ticks from -- "0"
    # (the model default) means "never read, start from the beginning."
    tick_cursor: Mapped[str] = mapped_column(String(64), nullable=False, default="0")

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def trading_symbol(self) -> str:
        """What is quoted, ticked, filled and held: the contract for a future, else the symbol."""
        if self.instrument_kind == "future" and self.contract_symbol:
            return self.contract_symbol
        return self.symbol

    @property
    def is_future(self) -> bool:
        return self.instrument_kind == "future"

    def __repr__(self) -> str:
        return (
            f"PaperTradingSubscription(symbol={self.symbol!r}, "
            f"strategy_version_id={self.strategy_version_id!r})"
        )
