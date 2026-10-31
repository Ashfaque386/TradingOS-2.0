"""the live ledger is built from confirmed fills, one position per strategy and symbol

A live position used to move the moment an order was sent, at the price it was priced against, so a
rejected or cancelled order left a position the account never held, and a fill at a different price
was never used. It now moves only when the broker confirms the fill, at the broker's price, and
records an estimate of what the fill cost. A strategy can also trade more than one symbol over its
life, so the unique key is the strategy and the symbol, not the strategy alone (every existing row
already carries its symbol).

Revision ID: f8d2b6a1c4e7
Revises: e7c1a4b9d2f6
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8d2b6a1c4e7"
down_revision: str | None = "e7c1a4b9d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trades",
        sa.Column("costs_total", sa.Float(), server_default="0", nullable=False),
    )
    op.add_column(
        "live_positions",
        sa.Column("costs_total", sa.Float(), server_default="0", nullable=False),
    )
    op.drop_constraint("uq_live_positions_strategy_id", "live_positions", type_="unique")
    op.create_unique_constraint(
        "uq_live_positions_strategy_symbol", "live_positions", ["strategy_id", "symbol"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_live_positions_strategy_symbol", "live_positions", type_="unique")
    op.create_unique_constraint("uq_live_positions_strategy_id", "live_positions", ["strategy_id"])
    op.drop_column("live_positions", "costs_total")
    op.drop_column("trades", "costs_total")
