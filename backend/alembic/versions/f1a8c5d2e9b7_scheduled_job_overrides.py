"""scheduled_job_overrides: a schedule an administrator chose, kept across a restart

A schedule edit in the console used to change the running job only, so the next restart put the
code's cadence back without a word. One row per job; a restart applies it on top of the default.

Revision ID: f1a8c5d2e9b7
Revises: e7c3a1b9d5f2
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a8c5d2e9b7"
down_revision: str | None = "e7c3a1b9d5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_job_overrides",
        sa.Column("scheduler", sa.String(length=48), nullable=False),
        sa.Column("job_id", sa.String(length=96), nullable=False),
        sa.Column("seconds", sa.Integer(), nullable=True),
        sa.Column("hour", sa.Integer(), nullable=True),
        sa.Column("minute", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "(seconds IS NOT NULL AND hour IS NULL AND minute IS NULL)"
            " OR (seconds IS NULL AND hour IS NOT NULL AND minute IS NOT NULL)",
            name="ck_scheduled_job_overrides_one_kind",
        ),
        sa.CheckConstraint(
            "seconds IS NULL OR seconds >= 1", name="ck_scheduled_job_overrides_seconds"
        ),
        sa.CheckConstraint(
            "hour IS NULL OR (hour >= 0 AND hour <= 23)", name="ck_scheduled_job_overrides_hour"
        ),
        sa.CheckConstraint(
            "minute IS NULL OR (minute >= 0 AND minute <= 59)",
            name="ck_scheduled_job_overrides_minute",
        ),
        sa.PrimaryKeyConstraint("scheduler", "job_id"),
    )


def downgrade() -> None:
    op.drop_table("scheduled_job_overrides")
