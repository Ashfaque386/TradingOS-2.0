import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class InvestorReport(Base):
    """Investor Reporting Agent output (Phase 19, docs/phase19-audit.md
    Part 2.5): a real markdown performance narrative generated on a
    configurable cadence (weekly by default), sourced from the same real
    P&L figures every other report in this app uses
    (`PaperFill.realized_pnl` via the same aggregate `/pnl/today` already
    computes, plus win-rate from `TradeReviewFinding` rows) -- never a
    fabricated number. Historical reports are kept, not overwritten, so
    the frontend can browse past periods, not just the latest.
    """

    __tablename__ = "investor_reports"
    __table_args__ = (
        CheckConstraint("cadence IN ('weekly','monthly')", name="ck_investor_reports_cadence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="weekly")

    content_markdown: Mapped[str] = mapped_column(Text, nullable=False)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    generated_by: Mapped[str] = mapped_column(
        String(64), nullable=False, default="system:investor-reporting-agent"
    )

    def __repr__(self) -> str:
        return f"InvestorReport(period_end={self.period_end!r}, cadence={self.cadence!r})"
