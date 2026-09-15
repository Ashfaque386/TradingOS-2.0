import uuid

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class AgentToAgentPolicy(Base, TimestampMixin):
    """One row per explicit allow rule in config.agentToAgentPolicy.allow
    (Build Spec §5.1/§6.4). Default-deny is enforced in code
    (src/orchestration/handoffs.py), never as a row here — the absence of
    a matching row IS the deny. Kept in sync by src/gateway/apply.py on
    every successful config application — never written directly.
    """

    __tablename__ = "agent_to_agent_policy"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    from_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_agent: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)

    def __repr__(self) -> str:
        return f"AgentToAgentPolicy({self.from_agent!r} -> {self.to_agent!r}, {self.scope!r})"
