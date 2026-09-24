import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PromptVersionStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class PromptVersion(Base):
    """One immutable prompt revision for one roster agent (Build Spec
    §7.1). `content` is set once at INSERT and never updated afterwards --
    the only writes this module makes to an existing row are status/
    diff_from_previous/activated_at, via activate_prompt_version()
    (src/orchestration/prompt_versions.py). A revision is retired by
    activating a different one, never edited in place.

    Activation is diff-gated: activate_prompt_version() always computes and
    stores the unified diff against whichever version was previously ACTIVE
    for this agent as part of the same call that flips status -- there is
    no function that activates a version without that diff having been
    produced. Rollback is the same call, targeting an older (SUPERSEDED)
    version_id.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("agent_id", "version_number", name="uq_prompt_version_agent_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_identities.agent_id"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[PromptVersionStatus] = mapped_column(
        Enum(
            PromptVersionStatus,
            name="prompt_version_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        default=PromptVersionStatus.DRAFT,
    )
    diff_from_previous: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"PromptVersion(agent_id={self.agent_id!r}, "
            f"version_number={self.version_number!r}, status={self.status!r})"
        )
