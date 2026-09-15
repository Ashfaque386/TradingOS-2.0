import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin
from src.models.organization_run import FailureClass


class TaskStatus(StrEnum):
    PENDING = "pending"  # waiting on unsatisfied dependencies
    READY = "ready"  # dependencies satisfied, awaiting claim
    CLAIMED = "claimed"  # claimed by a worker, about to dispatch
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNSATISFIABLE = "unsatisfiable"  # an upstream dependency permanently failed


class Task(Base, TimestampMixin):
    """One node of a plan's task graph (src/orchestration/planner.py
    materializes these from an accepted OrganizationalPlan). Claimed and
    executed by src/orchestration/task_engine.py.
    """

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=False, index=True
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizational_plans.id"), nullable=False
    )
    # The TaskSpec.key it was materialized from — a stable, human-readable
    # local identifier for the task within its plan (dependency wiring, logs,
    # debugging), distinct from the DB-generated `id`.
    plan_key: Mapped[str] = mapped_column(String(128), nullable=False)
    capability: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # From TaskSpec.produces_artefact_type at plan time — what artefact_type
    # the task engine tags this task's ResultArtefact with on success, so a
    # dependent task's typed DependencySpec.artefact_type can match it.
    produces_artefact_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(
            TaskStatus,
            name="task_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
        default=TaskStatus.PENDING,
    )
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[FailureClass | None] = mapped_column(
        Enum(
            FailureClass,
            name="failure_class",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=True,
    )
    # Set once by the 5-minute stall sweep (Build Spec §7.3) the first time a
    # RUNNING task is found to have been running >900s; prevents re-flagging
    # (and re-emitting an event for) the same stall on every subsequent sweep.
    stalled_flagged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return f"Task(id={self.id!r}, capability={self.capability!r}, status={self.status!r})"
