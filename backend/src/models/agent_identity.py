from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class AgentIdentity(Base, TimestampMixin):
    """One row per fixed-roster agent (Build Spec §5.1), mirroring the
    identity fields (name/emoji/avatar/theme/voice) currently active in
    config/tradingos.config.json. Kept in sync by src/gateway/apply.py on
    every successful config application — never written directly.
    """

    __tablename__ = "agent_identities"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True)
    avatar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    theme: Mapped[str | None] = mapped_column(String(32), nullable=True)
    voice: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"AgentIdentity(agent_id={self.agent_id!r}, name={self.name!r})"
