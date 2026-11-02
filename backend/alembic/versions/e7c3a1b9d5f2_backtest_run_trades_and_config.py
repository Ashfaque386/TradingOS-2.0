"""backtest_runs: each trade with its dates, and how the signals were made

A run kept only the net result of each trade, so a ledger could not say when anything was bought
or sold, and a later walk-forward had to be told again which strategy and window the run used.
Both columns are additive and nullable; a run from before this has neither.

Revision ID: e7c3a1b9d5f2
Revises: d6b2f0a8c3e5
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a1b9d5f2"
down_revision: str | None = "d6b2f0a8c3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_runs", sa.Column("trades", sa.JSON(), nullable=True))
    op.add_column("backtest_runs", sa.Column("signal_config", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_runs", "signal_config")
    op.drop_column("backtest_runs", "trades")
