from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ConfigVersionStatus(StrEnum):
    ACTIVE = "active"
    REJECTED = "rejected"


class AgentConfigVersion(Base):
    """Version history of config/tradingos.config.json (Build Spec §5.1/§6.2).
    Write-once: a row is never updated after insert, only ever superseded
    by a new one — `id` doubles as the version number CLI/API rollback
    refers to (`tradingos-cli config rollback --to-version <id>`).
    """

    __tablename__ = "agent_config_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    status: Mapped[ConfigVersionStatus] = mapped_column(
        Enum(
            ConfigVersionStatus,
            name="config_version_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    raw_content: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"AgentConfigVersion(id={self.id!r}, status={self.status!r})"
