import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class AgentPipelineEvent(Base):
    """One row per step of one LangGraph pipeline run (Phase 19 Finding #1:
    the live per-step agent feed -- "agent X is currently doing Y").

    Deliberately NOT `organizational_events`: that table's `run_id` is a
    foreign key to `organization_runs`, the Phase 2 task-graph engine, which
    the LangGraph pipeline (`src.agents.graph`) has never been part of --
    this codebase has two architecturally distinct "run" systems. `run_id`
    here is a plain id minted per `POST /agents/pipeline/run` call, with no
    FK, so a pipeline run never has to masquerade as an organization run
    (and never appears on the Mission Control Kanban as one).

    `sequence` is gap-free per `run_id` without a lock: a single pipeline
    run executes its nodes sequentially in one process, so there is exactly
    one writer per `run_id` (see `src.orchestration.pipeline_events`).
    """

    __tablename__ = "agent_pipeline_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        Index("ix_agent_pipeline_events_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # pipeline.started / pipeline.completed / pipeline.failed /
    # step.started / step.completed / step.failed / step.skipped
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"AgentPipelineEvent(run_id={self.run_id!r}, sequence={self.sequence!r}, "
            f"event_type={self.event_type!r}, node={self.node!r})"
        )
