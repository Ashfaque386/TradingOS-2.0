import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class PortfolioRecommendationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class PortfolioRecommendation(Base, TimestampMixin):
    """One real, advisory-only rebalancing recommendation (Phase 24,
    docs/phase20-old-vs-new-comparison.md item 22): composed from real
    active-strategy backtest metrics and, when a broker is configured,
    real margin -- never a fabricated allocation. `allocations` is a
    deterministic rule over each strategy's real `sharpe`/`max_drawdown`
    (see `src.orchestration.portfolio_advisor`), not something the LLM
    invents from scratch -- a financial suggestion this codebase asks an
    operator to act on needs an allocation decision that's auditable back
    to real numbers, the same reasoning `src.orchestration.strategy_suggestions`
    already applies to code-review verdicts. The LLM's role is narrower and
    explicitly labeled: producing `summary`, a human-readable narrative
    over those same real numbers -- `llm_source` records whether a real
    provider produced it or every provider was unreachable and a plain
    fallback narrative was used instead (never silently presented as if a
    model wrote it).

    Exactly like `OperatorGuidanceDto`'s guidance-only status ("the CEO
    Agent folds this into its next planning cycle" -- never auto-applied),
    accepting or rejecting a recommendation here never triggers any order,
    strategy promotion, or config change by itself: `accept`/`reject`
    only record an operator's decision and stamp `reviewed_by`/`reviewed_at`
    for the audit trail (Non-Negotiable Rule #1 -- no path from this table
    to autonomous execution exists anywhere in this codebase).
    """

    __tablename__ = "portfolio_recommendations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    allocations: Mapped[list] = mapped_column(JSON, nullable=False)
    based_on: Mapped[dict] = mapped_column(JSON, nullable=False)
    llm_source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[PortfolioRecommendationStatus] = mapped_column(
        Enum(
            PortfolioRecommendationStatus,
            name="portfolio_recommendation_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        default=PortfolioRecommendationStatus.PENDING,
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"PortfolioRecommendation(id={self.id!r}, status={self.status!r})"
