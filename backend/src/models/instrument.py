import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, Float, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Instrument(Base):
    """Instrument master (Build Spec §14, §5.1 reference data): synced on
    a schedule (`src.orchestration.market_data.run_instrument_master_sync`),
    not manual-only -- an explicit fix vs. a prior build where this pipeline
    was dormant.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint(
            "instrument_type IN ('equity','future','option','index')",
            name="ck_instruments_type",
        ),
        CheckConstraint(
            "option_type IN ('CE','PE') OR option_type IS NULL", name="ck_instruments_option_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(16), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)

    underlying_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    strike_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    option_type: Mapped[str | None] = mapped_column(String(2), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"Instrument(symbol={self.symbol!r}, type={self.instrument_type!r})"
