"""equity snapshots, and a way to turn down a staged risk-limit change

`equity_snapshots` is the history the automatic kill switch reads and the console charts: one row
per reading of a mode's equity, NULL (never zero) when it could not be measured.

A staged or confirmed risk-limit change could only ever be applied or left to sit; the status
already allowed 'rejected' but nothing could set it. The request keeps who turned it down, when
and why.

Revision ID: e7c1a4b9d2f6
Revises: d3a9c5e7b1f2
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7c1a4b9d2f6"
down_revision: str | None = "d3a9c5e7b1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equity_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column(
            "taken_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("equity", sa.Float(), nullable=True),
        sa.Column("capital_base", sa.Float(), nullable=True),
        sa.Column("realized_net", sa.Float(), nullable=True),
        sa.Column("unrealized", sa.Float(), nullable=True),
        sa.Column("open_positions", sa.Integer(), nullable=False),
        sa.Column("unpriced_positions", sa.Integer(), nullable=False),
        sa.Column("price_source", sa.String(length=16), nullable=False),
        sa.Column("peak_equity", sa.Float(), nullable=True),
        sa.Column("drawdown_pct", sa.Float(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint("mode IN ('live','paper')", name="ck_equity_snapshots_mode"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_equity_snapshots_mode_taken_at", "equity_snapshots", ["mode", "taken_at"]
    )

    op.add_column(
        "risk_limit_change_requests", sa.Column("rejected_by", sa.String(length=128), nullable=True)
    )
    op.add_column(
        "risk_limit_change_requests",
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "risk_limit_change_requests", sa.Column("rejection_reason", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("risk_limit_change_requests", "rejection_reason")
    op.drop_column("risk_limit_change_requests", "rejected_at")
    op.drop_column("risk_limit_change_requests", "rejected_by")
    op.drop_index("ix_equity_snapshots_mode_taken_at", table_name="equity_snapshots")
    op.drop_table("equity_snapshots")
