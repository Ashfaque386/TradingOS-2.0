import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class TaskDependency(Base):
    """A typed artefact dependency edge: `task_id` waits on
    `depends_on_task_id` (Build Spec §7.3 dependency resolver). When
    `artefact_type` is set, satisfaction additionally requires a matching
    ResultArtefact from the upstream task, not just its success — see
    src/orchestration/dependencies.py.
    """

    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_task_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False, index=True
    )
    artefact_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"TaskDependency(task_id={self.task_id!r}, depends_on={self.depends_on_task_id!r})"
