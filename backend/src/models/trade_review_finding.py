import uuid
from datetime import date, datetime

from sqlalchemy import (
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


class TradeReviewFinding(Base):
    """Post-Trade Review Agent output (Phase 19, docs/phase19-audit.md Part
    2.4): one row per strategy per review run, reviewing that strategy's
    closed trades against real, already-computed figures -- never a
    re-derived P&L. Paper trades use `PaperFill.realized_pnl` (Phase 7's
    average-cost-basis accounting, the same field `/pnl/today` aggregates
    globally); live trades have no per-trade realized-P&L field anywhere
    in this codebase (`Trade` only carries `price`/`fill_price`), so
    `pnl_scope='cumulative_to_date'` for a live-mode row reads
    `LivePosition.realized_pnl` (a running total) instead of a same-day
    figure -- `pnl_scope` exists specifically so a reader can never
    mistake one for the other. `win_rate`/`winning_trades`/`losing_trades`
    are left NULL for live-mode rows for the same honesty reason: without
    a per-trade realized figure there is no real win/loss split to report,
    only a fill count.

    `src.orchestration.strategy_suggestions.review_suggestion` (the real,
    already-existing "next cycle" the Strategy Generator/Evaluator agents
    run through for an existing strategy) reads the latest row for a
    strategy and folds `commentary` into its LLM prompt -- see that
    module's own docstring for the wiring.
    """

    __tablename__ = "trade_review_findings"
    __table_args__ = (
        CheckConstraint("mode IN ('paper','live')", name="ck_trade_review_findings_mode"),
        CheckConstraint(
            "pnl_scope IN ('today','cumulative_to_date')", name="ck_trade_review_findings_pnl_scope"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    review_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False)

    trades_reviewed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    losing_trades: Mapped[int | None] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[float | None] = mapped_column(Float, nullable=True)

    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl_scope: Mapped[str] = mapped_column(String(24), nullable=False)

    commentary: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"TradeReviewFinding(strategy_id={self.strategy_id!r}, "
            f"review_date={self.review_date!r}, mode={self.mode!r})"
        )
