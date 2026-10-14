"""data_source provenance on backtest runs, paper fills, trades and trade reviews

Nothing recorded whether a backtest, a paper fill, a live trade or a post-trade review
rested on real market data or on data this system generated. `data_source` does; rows
that already exist are `unknown`, because nobody can say.

Revision ID: d1f5a8c3b7e2
Revises: c4a7e1b95d03
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f5a8c3b7e2"
down_revision: str | None = "c4a7e1b95d03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "data_source IN ('real','synthetic','supplied','mixed','unknown')"
_TABLES = ("backtest_runs", "paper_fills", "trades", "trade_review_findings")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column("data_source", sa.String(16), nullable=False, server_default="unknown"),
        )
        op.create_check_constraint(f"ck_{table}_data_source", table, _CHECK)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_constraint(f"ck_{table}_data_source", table, type_="check")
        op.drop_column(table, "data_source")
