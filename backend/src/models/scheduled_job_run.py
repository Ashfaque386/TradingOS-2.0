import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ScheduledJobRunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ScheduledJobRun(Base):
    """One real firing of a real APScheduler job (Phase 22,
    docs/phase20-old-vs-new-comparison.md item 20) -- written by
    `src.observability.scheduled_job_history`'s `EVENT_JOB_EXECUTED` /
    `EVENT_JOB_ERROR` listener, attached to every scheduler this app starts
    the same way `src.observability.scheduler_registry` already tracks the
    schedulers themselves.

    APScheduler's own in-memory job store never remembers a firing once it
    completes, so `GET /system/scheduled-jobs/.../history` would otherwise
    have nothing real to read -- these rows are that missing durable
    record, not a UI-only cache. Bounded per (scheduler, job_id) to the
    most recent 50 rows at write time (see
    `scheduled_job_history._prune_old_runs`) rather than kept forever,
    since nothing in this codebase reads further back than that and these
    jobs fire at most a few times a minute.
    """

    __tablename__ = "scheduled_job_runs"
    __table_args__ = (
        Index("ix_scheduled_job_runs_scheduler_job", "scheduler", "job_id", "scheduled_run_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scheduler: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(128), nullable=False)
    scheduled_run_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[ScheduledJobRunStatus] = mapped_column(
        Enum(
            ScheduledJobRunStatus,
            name="scheduled_job_run_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"ScheduledJobRun(scheduler={self.scheduler!r}, job_id={self.job_id!r}, "
            f"status={self.status!r})"
        )
