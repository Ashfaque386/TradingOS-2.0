import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class RiskLimitChangeStatus:
    """Plain string constants, not a StrEnum -- mirrors Strategy.status's
    String+CheckConstraint choice for the same small-fixed-set-of-strings
    column (avoids the native-enum-not-dropped-on-downgrade defect class
    that hit earlier migrations in this project)."""

    STAGED = "staged"
    CONFIRMED = "confirmed"
    APPLIED = "applied"
    REJECTED = "rejected"


class RiskLimit(Base):
    """The current effective value of one named, dual-control-gated risk
    limit (Build Spec §8.5) -- e.g. `max_drawdown_pct`, `ws_latency_ms`.
    This table, plus `RiskLimitChangeRequest`'s stage -> confirm -> apply
    flow (src.orchestration.risk_limits), is the ONLY way a value backing
    `infra.riskThresholdRefs` (src.gateway.schema) actually changes -- there
    is no other write path anywhere in this codebase."""

    __tablename__ = "risk_limits"
    __table_args__ = (UniqueConstraint("name", name="uq_risk_limits_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"RiskLimit(name={self.name!r}, value={self.value!r})"


class RiskLimitChangeRequest(Base):
    """One proposed change to a named risk limit, moving staged ->
    confirmed -> applied. `confirmed_by` must differ from `staged_by`
    (enforced in src.orchestration.risk_limits.confirm_risk_limit_change,
    not just by RBAC role) -- a self-confirmed change is never valid
    regardless of the staging user's role."""

    __tablename__ = "risk_limit_change_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('staged','confirmed','applied','rejected')",
            name="ck_risk_limit_change_requests_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    limit_name: Mapped[str] = mapped_column(String(64), nullable=False)
    proposed_value: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=RiskLimitChangeStatus.STAGED
    )

    staged_by: Mapped[str] = mapped_column(String(128), nullable=False)
    staged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    confirmed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    applied_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"RiskLimitChangeRequest(limit_name={self.limit_name!r}, status={self.status!r})"
