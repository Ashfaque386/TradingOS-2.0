"""what the Go-Live evidence needs to tell a trade from a cleanup, and a clean day from a silent one

`paper_fills.administrative` marks a close done by hand without the risk gate, which is not a trade the
strategy made. `shadow_mode_runs` gets the strategy a check counts toward, whether it worked, and why
it did not: a dry run that raised left no row at all, so "clean" could not be told from "never tried".
Existing rows are all runs that worked (a failure left nothing behind).

Revision ID: a9e3c7f1d5b8
Revises: f8d2b6a1c4e7
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a9e3c7f1d5b8"
down_revision: str | None = "f8d2b6a1c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_fills",
        sa.Column("administrative", sa.Boolean(), server_default="false", nullable=False),
    )
    op.add_column(
        "shadow_mode_runs",
        sa.Column("strategy_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "shadow_mode_runs",
        sa.Column("outcome", sa.String(length=16), server_default="ok", nullable=False),
    )
    op.add_column("shadow_mode_runs", sa.Column("failure_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "shadow_mode_runs_strategy_id_fkey",
        "shadow_mode_runs",
        "strategies",
        ["strategy_id"],
        ["id"],
    )
    op.create_index("ix_shadow_mode_runs_strategy_id", "shadow_mode_runs", ["strategy_id"])
    op.create_check_constraint(
        "ck_shadow_mode_runs_outcome", "shadow_mode_runs", "outcome IN ('ok','failed')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_shadow_mode_runs_outcome", "shadow_mode_runs", type_="check")
    op.drop_index("ix_shadow_mode_runs_strategy_id", table_name="shadow_mode_runs")
    op.drop_constraint("shadow_mode_runs_strategy_id_fkey", "shadow_mode_runs", type_="foreignkey")
    op.drop_column("shadow_mode_runs", "failure_reason")
    op.drop_column("shadow_mode_runs", "outcome")
    op.drop_column("shadow_mode_runs", "strategy_id")
    op.drop_column("paper_fills", "administrative")
