"""audit_log: indexes for the two lookups the console makes

The Audit page filters by actor and traces one entity (`entity_id`); both read the whole table
without an index, and the table only grows. Indexes are additive and change no row, so the hash
chain and its append-only trigger are untouched.

Revision ID: d6b2f0a8c3e5
Revises: c5a1e9f3b7d4
Create Date: 2026-11-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d6b2f0a8c3e5"
down_revision: str | None = "c5a1e9f3b7d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(op.f("ix_audit_log_actor"), "audit_log", ["actor"], unique=False)
    op.create_index(op.f("ix_audit_log_entity_id"), "audit_log", ["entity_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_log_entity_id"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_actor"), table_name="audit_log")
