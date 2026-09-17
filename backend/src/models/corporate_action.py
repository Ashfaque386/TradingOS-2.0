import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Float, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class CorporateAction(Base):
    """Corporate actions ingested on a schedule (Build Spec §14). The
    `UniqueConstraint` below is what makes re-running the ingestion job
    idempotent -- an upsert (`INSERT ... ON CONFLICT DO UPDATE`) on the
    same (symbol, action_type, ex_date) never creates a duplicate row.
    """

    __tablename__ = "corporate_actions"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('dividend','split','bonus','rights')",
            name="ck_corporate_actions_type",
        ),
        UniqueConstraint(
            "symbol", "action_type", "ex_date", name="uq_corporate_actions_symbol_type_exdate"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    announced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"CorporateAction(symbol={self.symbol!r}, type={self.action_type!r}, "
            f"ex_date={self.ex_date!r})"
        )
