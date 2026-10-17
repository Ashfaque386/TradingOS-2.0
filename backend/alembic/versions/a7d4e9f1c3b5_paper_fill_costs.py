"""what a paper fill costs: per-fill costs and a running total per position

Paper fills used to be gross: no brokerage, STT, exchange charges, SEBI fee, stamp duty or GST,
so every result flattered the strategy. A fill now records `costs_total` and an itemised
`costs_breakdown`; the position keeps the running `costs_total` beside its (still gross)
`realized_pnl`, so net = realized_pnl - costs_total. Existing rows get 0 costs: they were never
charged, and are not back-dated.

Revision ID: a7d4e9f1c3b5
Revises: f6c3d8e0b2a4
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d4e9f1c3b5"
down_revision: str | None = "f6c3d8e0b2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_fills",
        sa.Column("costs_total", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("paper_fills", sa.Column("costs_breakdown", sa.JSON(), nullable=True))
    op.add_column(
        "paper_positions",
        sa.Column("costs_total", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("paper_positions", "costs_total")
    op.drop_column("paper_fills", "costs_breakdown")
    op.drop_column("paper_fills", "costs_total")
