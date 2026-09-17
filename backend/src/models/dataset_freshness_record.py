import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class DatasetFreshnessRecord(Base):
    """One row per (symbol, data_type, data_date) actually landed in the
    Parquet data lake (Build Spec §14, §5.1 reference data) -- this table,
    not the Parquet files themselves, is what
    `src.data.freshness_snapshot.load_freshness_snapshot` queries to answer
    the data-freshness gate's "does the lake have this exact day's data"
    question. Kept in Postgres (fast, async-native, no DuckDB file-lock
    concerns) rather than re-derived from DuckDB on every gate check; each
    write here happens immediately after -- and is only written after --
    `src.data.lake.has_data_for` confirms the corresponding write actually
    landed and is readable back, so this table can never claim freshness
    the Parquet store doesn't actually have.
    """

    __tablename__ = "dataset_freshness_records"
    __table_args__ = (
        CheckConstraint(
            "data_type IN ('daily_ohlcv','intraday_ohlcv')",
            name="ck_dataset_freshness_records_data_type",
        ),
        UniqueConstraint(
            "symbol", "data_type", "data_date", name="uq_dataset_freshness_records_symbol_type_date"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    data_type: Mapped[str] = mapped_column(String(16), nullable=False)
    data_date: Mapped[date] = mapped_column(Date, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"DatasetFreshnessRecord(symbol={self.symbol!r}, data_type={self.data_type!r}, "
            f"data_date={self.data_date!r})"
        )
