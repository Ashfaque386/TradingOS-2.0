from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ScheduledJobOverride(Base):
    """A schedule an administrator chose for a scheduled job, kept so it survives a restart.

    The jobs are registered by code with a cadence of their own. A change made in the console used
    to be applied to the running job only, so the next restart put the code's cadence back and said
    nothing. This row is what a restart applies on top of the code's default
    (`src.orchestration.job_schedule_store.apply_stored_overrides`); deleting it, "restore the
    default", puts the code's cadence back.

    One row per job. An interval job has `seconds`; a cron job has `hour` and `minute` (the other
    fields of its expression, such as the day of the week, stay as the code set them).
    """

    __tablename__ = "scheduled_job_overrides"
    __table_args__ = (
        CheckConstraint(
            "(seconds IS NOT NULL AND hour IS NULL AND minute IS NULL)"
            " OR (seconds IS NULL AND hour IS NOT NULL AND minute IS NOT NULL)",
            name="ck_scheduled_job_overrides_one_kind",
        ),
        CheckConstraint(
            "seconds IS NULL OR seconds >= 1", name="ck_scheduled_job_overrides_seconds"
        ),
        CheckConstraint(
            "hour IS NULL OR (hour >= 0 AND hour <= 23)", name="ck_scheduled_job_overrides_hour"
        ),
        CheckConstraint(
            "minute IS NULL OR (minute >= 0 AND minute <= 59)",
            name="ck_scheduled_job_overrides_minute",
        ),
    )

    scheduler: Mapped[str] = mapped_column(String(48), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"ScheduledJobOverride({self.scheduler!r}/{self.job_id!r})"
