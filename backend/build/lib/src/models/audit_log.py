import uuid
from datetime import datetime

from sqlalchemy import DDL, JSON, BigInteger, DateTime, String, UniqueConstraint, event, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

# The DB-level append-only enforcement (Build Spec §19): no UPDATE or
# DELETE against audit_log can ever succeed, regardless of role or code
# path, short of a superuser first dropping this trigger. Defined once
# here and reused by both this ORM-metadata `after_create` hook (fires
# when tests build the schema via `Base.metadata.create_all`, which --
# unlike Alembic -- has no way to run arbitrary DDL on its own) and the
# Phase 11 Alembic migration (`ce1b6bee5a66_audit_log_hash_chain.py`,
# which imports these same two constants) -- a real deployment only ever
# runs the migration, but this keeps the two paths from being able to
# drift apart into two different trigger definitions.
CREATE_AUDIT_LOG_TRIGGER_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION reject_audit_log_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted on this table', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

CREATE_AUDIT_LOG_TRIGGER_SQL = """
CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION reject_audit_log_mutation();
"""


class AuditLog(Base):
    """The full Build Spec §19 audit trail: hash-chained (SHA-256),
    DB-trigger-enforced append-only (see the Phase 11 migration's
    `reject_audit_log_mutation()` trigger — no code path, not even a
    direct `UPDATE`/`DELETE` from psql as a superuser without first
    dropping the trigger, can alter or remove a row), advisory-lock
    -serialized writers (`src.audit.service.write_audit_entry`, the only
    function that may ever construct a row here — see its module
    docstring for why `sequence` is a single global chain, not per-entity).

    `sequence`/`previous_hash`/`hash` implement the hash chain itself
    (`src.audit.chain`); `correlation_id` is the Build Spec §19 "logging"
    requirement's persisted anchor -- the durable link between a live
    structlog correlation ID (present on the request/job's log lines
    while they're being emitted) and the audit trail (present forever).
    This table was first introduced in Phase 1 for Agent Gateway config
    apply/reject events, deliberately without the hash chain or trigger
    yet (see that phase's own note, now resolved) -- extended in place
    here rather than replaced by a second, parallel audit mechanism.
    """

    __tablename__ = "audit_log"
    __table_args__ = (UniqueConstraint("sequence", name="uq_audit_log_sequence"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"AuditLog(sequence={self.sequence!r}, actor={self.actor!r}, action={self.action!r})"


event.listen(
    AuditLog.__table__,
    "after_create",
    # SQLAlchemy's DDL construct treats a literal "%" in its statement as a
    # Python format placeholder when compiled against a table-bound event
    # (it substitutes %(table)s/%(schema)s/%(fullname)s) -- escaped to
    # "%%" here only, since this is the one execution path that goes
    # through that substitution; the Alembic migration's op.execute() call
    # (using this same CREATE_AUDIT_LOG_TRIGGER_FUNCTION_SQL constant,
    # unescaped) isn't bound to a table event and never hits it.
    DDL(CREATE_AUDIT_LOG_TRIGGER_FUNCTION_SQL.replace("%", "%%")).execute_if(dialect="postgresql"),
)
event.listen(
    AuditLog.__table__,
    "after_create",
    DDL(CREATE_AUDIT_LOG_TRIGGER_SQL).execute_if(dialect="postgresql"),
)
