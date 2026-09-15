import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class PlanStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class OrganizationalPlan(Base):
    """One planner attempt (src/orchestration/planner.py) for a run — up to
    3 rows per run before it's marked cannot_plan (Build Spec §7.3). Only an
    ACCEPTED plan has its tasks materialized into the `tasks` table.
    """

    __tablename__ = "organizational_plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organization_runs.id"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(
            PlanStatus,
            name="plan_status",
            native_enum=True,
            values_callable=lambda cls: [m.value for m in cls],
        ),
        nullable=False,
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    planner_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_errors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"OrganizationalPlan(id={self.id!r}, run_id={self.run_id!r}, "
            f"attempt={self.attempt_number!r})"
        )
