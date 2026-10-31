"""the kill switch remembers why it was reset

A reset needs a human, and the console asks that human for a reason, but the reason was sent and
dropped: nothing stored it, so the only record of why a tripped switch was re-armed was the actor's
name. The state row keeps the latest reason beside `reset_by` and `reset_at`.

Revision ID: d3a9c5e7b1f2
Revises: c9f6a1b3e5d7
Create Date: 2026-10-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3a9c5e7b1f2"
down_revision: str | None = "c9f6a1b3e5d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("kill_switch_states", sa.Column("reset_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("kill_switch_states", "reset_reason")
