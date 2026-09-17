import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class ChatSession(Base):
    """One in-app chat conversation (Build Spec §18's "in-app chat":
    streaming, abort, per-session model switch, search, pin, export).
    `model` holds a `src.gateway.schema.LlmProvider` value (or `None` for
    "auto", the router's default live-config fallback order) -- the
    per-session model switch this phase's requirements call for, passed
    as `LlmRouter.complete/stream_complete`'s `preferred_provider`
    without touching the global Gateway config.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="New chat")
    model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"ChatSession(id={self.id!r}, title={self.title!r}, pinned={self.pinned!r})"
