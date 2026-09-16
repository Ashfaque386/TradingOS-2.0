import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class StrategyVersion(Base):
    """One generated code revision for a Strategy (Build Spec §9). `code`
    is written once at INSERT and never updated afterwards -- the same
    immutability contract as Phase 3's PromptVersion. A regenerated version
    (from static validation failure, or from the human-suggestions flow,
    src/orchestration/strategy_suggestions.py) is always a new row with the
    next `version_number`, never an edit of an existing one.

    `static_validation_*` is always populated at INSERT (Build Spec §9:
    "before anything reaches the sandbox"). `sandbox_*` starts NULL and is
    filled in by a later, separate call once the version has actually run
    inside the sandbox -- a version can be statically valid but never make
    it to (or fail inside) the sandbox.
    """

    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version_number", name="uq_strategy_version_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    code: Mapped[str] = mapped_column(Text, nullable=False)

    static_validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    static_validation_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)

    sandbox_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sandbox_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Only populated for InstrumentClass.OPTIONS strategies -- grounded legs
    # (src/engine/options_grounding.py) plus the naked-options scan verdict,
    # both required before acceptance (Build Spec §9).
    options_legs: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"StrategyVersion(strategy_id={self.strategy_id!r}, "
            f"version_number={self.version_number!r})"
        )
