import uuid
from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class JwtSigningKey(Base):
    """The live HS256 signing secret for every access/refresh JWT this app
    issues (Phase 21, docs/phase20-old-vs-new-comparison.md item 11) --
    genuinely rotatable at runtime, not just an env var a restart would be
    needed to pick up.

    Exactly one row ever matters: `get_current_signing_key()`
    (src.core.security) always reads the most recently created row, never
    an id passed in. Rotating never deletes an old row -- kept only as a
    durable audit trail of when a rotation happened and who did it; an old
    row's `key` is never read again once a newer one exists, so retaining
    it creates no way for a stale key to be accepted after rotation.

    Seeded lazily, once, the first time `get_current_signing_key()` finds
    no row at all: from `settings.jwt_secret_key` (the pre-Phase-21 env
    var), not a fresh random value -- so every session/token issued before
    this phase's deployment keeps validating across the upgrade, and a
    rotation is a deliberate, separate, audited action, never an implicit
    side effect of this table simply coming into existence.
    """

    __tablename__ = "jwt_signing_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # "env-seed" for the one lazily-created bootstrap row, else the email
    # of the SystemAdministrator who rotated it.
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)

    def __repr__(self) -> str:
        return f"JwtSigningKey(id={self.id!r}, created_at={self.created_at!r})"
