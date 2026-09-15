import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class AgentBinding(Base, TimestampMixin):
    """Channel -> persona routing (Build Spec §5.1/§6.1 `bindings`), mirroring
    config/tradingos.config.json. Kept in sync by src/gateway/apply.py on
    every successful config application — never written directly.
    """

    __tablename__ = "agent_bindings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("agent_identities.agent_id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    def __repr__(self) -> str:
        return f"AgentBinding(agent_id={self.agent_id!r}, channel={self.channel!r})"
