import uuid
from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class SuggestionStatus:
    """Plain string constants, not a StrEnum -- mirrors Strategy.status's
    String+CheckConstraint choice (see that model's docstring) for the same
    small-fixed-set-of-strings column.
    """

    PENDING = "pending"
    REVIEWED = "reviewed"
    REGENERATED = "regenerated"
    DISMISSED = "dismissed"


class StrategySuggestion(Base):
    """Human free-text improvement request against one StrategyVersion
    (Build Spec §9 "Human suggestions"): free-text -> AI review verdict ->
    optional regeneration, diff-gated the same way as Phase 3's prompt
    versioning (src/orchestration/strategy_suggestions.py).
    """

    __tablename__ = "strategy_suggestions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','reviewed','regenerated','dismissed')",
            name="ck_strategy_suggestions_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False, index=True
    )
    base_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=False
    )
    suggestion_text: Mapped[str] = mapped_column(Text, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(128), nullable=True)

    ai_verdict: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    regenerated_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=True
    )
    # Always populated together with regenerated_version_id, in the same
    # call (src.orchestration.strategy_suggestions.regenerate_from_suggestion)
    # -- a unified diff against base_version_id's code, the same
    # diff-gating contract as Phase 3's PromptVersion.diff_from_previous.
    # There is no regeneration path that can set regenerated_version_id
    # without this also being set.
    regeneration_diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SuggestionStatus.PENDING
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"StrategySuggestion(id={self.id!r}, strategy_id={self.strategy_id!r}, "
            f"status={self.status!r})"
        )
