import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base, TimestampMixin


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    CANNOT_PLAN = "cannot_plan"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


# A run left in one of these the instant the app boots can only be there
# because a prior process crashed mid-flight — nothing else could still be
# legitimately "in progress" at that moment. src/orchestration/recovery.py's
# reap_incomplete_runs() re-enters exactly these. PAUSED is deliberately
# excluded: it's a human decision, not a crash artefact, and reaping must
# never override it.
NON_TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.PENDING, RunStatus.PLANNING, RunStatus.RUNNING}
)
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.CANNOT_PLAN, RunStatus.COMPLETED, RunStatus.FAILED}
)


class RunType(StrEnum):
    STANDARD = "standard"
    RERUN = "rerun"


class RunSource(StrEnum):
    UI = "ui"
    SCHEDULE = "schedule"
    CHAT = "chat"
    WEBHOOK = "webhook"


class FailureClass(StrEnum):
    """Transient-vs-permanent classification (Build Spec §7.3 run control):
    retry_run() only allows a retry when a run's failure_class is TRANSIENT.
    """

    TRANSIENT = "transient"
    PERMANENT = "permanent"


def _native_enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class OrganizationRun(Base, TimestampMixin):
    """One CEO-organization run: an objective, planned into a task graph
    (src/orchestration/planner.py) and executed against capabilities
    (src/orchestration/task_engine.py) — stub capabilities in this phase,
    real 24-agent-backed ones from Phase 3 onward.
    """

    __tablename__ = "organization_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[RunSource] = mapped_column(_native_enum(RunSource, "run_source"), nullable=False)
    run_type: Mapped[RunType] = mapped_column(
        _native_enum(RunType, "run_type"), nullable=False, default=RunType.STANDARD
    )
    status: Mapped[RunStatus] = mapped_column(
        _native_enum(RunStatus, "run_status"), nullable=False, default=RunStatus.PENDING
    )
    # Rerun lineage (run_control.rerun_run): the fresh run this points back
    # to. run_type on the new row is always RUN_TYPE.RERUN, set explicitly —
    # never copied from source_run.run_type.
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=True
    )
    failure_class: Mapped[FailureClass | None] = mapped_column(
        _native_enum(FailureClass, "failure_class"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"OrganizationRun(id={self.id!r}, status={self.status!r})"
