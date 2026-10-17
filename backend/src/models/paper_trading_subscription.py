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
            "instrument_kind IN ('equity','future','option')",
            name="ck_paper_trading_subscriptions_instrument_kind",
        ),
        CheckConstraint(
            "instrument_kind <> 'future' OR (contract_symbol IS NOT NULL "
            "AND contract_expiry IS NOT NULL AND lot_size IS NOT NULL)",
            name="ck_paper_trading_subscriptions_future_has_contract",
        ),
        CheckConstraint(
            "instrument_kind <> 'option' OR option_strategy IN ('long_option','vertical_spread')",
            name="ck_paper_trading_subscriptions_option_has_strategy",
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
    # An `option` subscription reads signals from `symbol` (the underlying) like a future, but
    # holds whichever option legs its strategy chose (`long_option`: buy the at-the-money call on
    # a BUY signal, the put on a SELL; `vertical_spread`: the same as a debit spread, the bought
    # leg hedged by a sold one `spread_width` strikes further out). The legs are positions (one
    # row each), not a column here: they change with every entry.
    option_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    spread_width: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )

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
        """What is quoted, ticked, filled and held: the contract for a future, else the symbol.
        (An option subscription's own tick is its underlying's; its legs are held and quoted
        under their own contract names.)"""
        if self.instrument_kind == "future" and self.contract_symbol:
            return self.contract_symbol
        return self.symbol

    @property
    def is_future(self) -> bool:
        return self.instrument_kind == "future"

    @property
    def is_option(self) -> bool:
        return self.instrument_kind == "option"

    def __repr__(self) -> str:
        return (
            f"PaperTradingSubscription(symbol={self.symbol!r}, "
            f"strategy_version_id={self.strategy_version_id!r})"
        )
