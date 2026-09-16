import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ShadowModeConfidence:
    REAL_SANDBOX_DRY_RUN = "real_sandbox_dry_run"
    LOCAL_PAYLOAD_ONLY = "local_payload_only"


class ShadowModeRun(Base):
    """One Shadow Mode dry-run record (Build Spec §13,
    src.orchestration.shadow_mode). `confidence` is the honest
    differentiator this whole feature exists for: `real_sandbox_dry_run`
    means `broker_response` came back from a genuine network call against
    a broker's sandbox environment (Upstox); `local_payload_only` means
    no network call was made at all because the broker has no sandbox to
    call (Zerodha) -- `broker_response` is always NULL in that case, by
    construction, not because the call happened to fail. These two are
    never merged into one boolean "shadow mode ran successfully" -- a
    reader of this table (or the accompanying AuditLog entry) must be
    able to tell which kind of confidence they're looking at without
    guessing.
    """

    __tablename__ = "shadow_mode_runs"
    __table_args__ = (
        CheckConstraint("side IN ('buy','sell')", name="ck_shadow_mode_runs_side"),
        CheckConstraint(
            "confidence IN ('real_sandbox_dry_run','local_payload_only')",
            name="ck_shadow_mode_runs_confidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    broker_name: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    has_sandbox: Mapped[bool] = mapped_column(Boolean, nullable=False)
    confidence: Mapped[str] = mapped_column(String(32), nullable=False)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    order_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    broker_response: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"ShadowModeRun(broker_name={self.broker_name!r}, "
            f"confidence={self.confidence!r}, symbol={self.symbol!r})"
        )
