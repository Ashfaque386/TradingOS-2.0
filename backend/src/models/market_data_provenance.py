import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MarketDataProvenance(Base):
    """One row per ingestion-pipeline run (Build Spec §14, §5.1 reference
    data): the audit trail of what ran, when, against what source, and
    with what outcome -- e.g. distinguishing a real NSE Bhavcopy fetch from
    its synthetic fallback (`source`), the same honesty the Shadow Mode
    `confidence` field already enforces for broker dry-runs (Phase 8).
    """

    __tablename__ = "market_data_provenance"
    __table_args__ = (
        CheckConstraint(
            "pipeline IN ("
            "'incremental_daily','corporate_actions','instrument_master',"
            "'intraday_minute','bhavcopy_fallback','catalog_refresh','data_lake_backup'"
            ")",
            name="ck_market_data_provenance_pipeline",
        ),
        CheckConstraint(
            "status IN ('success','partial','failed')", name="ck_market_data_provenance_status"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    symbols_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_ingested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"MarketDataProvenance(pipeline={self.pipeline!r}, status={self.status!r})"
