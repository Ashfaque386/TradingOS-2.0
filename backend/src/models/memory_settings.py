from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MemorySettings(Base):
    """The operator's choice of embedding provider + model for agent
    long-term memory (src.memory), so it persists across restarts and image
    rebuilds without living in `.env`. A singleton: the CHECK pins `id` to 1,
    so there is at most one row and an upsert always targets it. When no row
    exists the service falls back to the MEMORY_EMBEDDING_* environment
    defaults. Deliberately holds no credentials -- those stay in the
    encrypted LLM provider store; a model name is not a secret, which is also
    why this can be read back to the UI.
    """

    __tablename__ = "memory_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_memory_settings_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False, default=1)
    embedding_provider: Mapped[str] = mapped_column(String(32), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(320), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
