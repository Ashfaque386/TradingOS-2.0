import uuid
from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class BacktestStatus:
    """Plain string constants, not a StrEnum -- mirrors Strategy.status's
    String+CheckConstraint choice (src/models/strategy.py) for the same
    small-fixed-set-of-strings column.
    """

    PENDING = "pending"
    COMPLETED = "completed"
    REFUSED_STALE_DATA = "refused_stale_data"
    FAILED = "failed"


class BacktestRun(Base):
    """One authoritative run of the vectorized backtest engine
    (src/engine/backtest/, Build Spec §10) against a StrategyVersion --
    distinct from that version's own self-reported sandbox_result
    (Phase 4): this is the platform's trusted, real-cost-modeled backtest,
    not the untrusted strategy code's self-report. Immutable once
    COMPLETED/FAILED/REFUSED_STALE_DATA -- there is no update path for
    metrics/daily_returns after creation, only new runs.
    """

    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','completed','refused_stale_data','failed')",
            name="ck_backtest_runs_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=BacktestStatus.PENDING)
    refusal_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    friction_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # [[iso_date, float], ...] -- the daily-returns series backing Sharpe/
    # MaxDD and src.engine.backtest.comparison's pairwise correlations.
    daily_returns: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # Per-trade net P&L, in trade order -- the resampling population for
    # src.engine.optimization.monte_carlo.run_monte_carlo.
    trade_pnls: Mapped[list | None] = mapped_column(JSON, nullable=True)

    walk_forward_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    monte_carlo_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"BacktestRun(id={self.id!r}, symbol={self.symbol!r}, status={self.status!r})"
