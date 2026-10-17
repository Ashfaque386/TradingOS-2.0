"""paper subscriptions can trade a monthly future: kind, contract, expiry, lot size, lots

A subscription traded `symbol` itself. A futures subscription keeps `symbol` as the underlying (its
signals come from the underlying's real history) and trades `contract_symbol`, rolled to the next
month as expiry nears. Existing rows are equity subscriptions with one lot, unchanged.

Revision ID: b8e5f0a2d4c6
Revises: a7d4e9f1c3b5
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e5f0a2d4c6"
down_revision: str | None = "a7d4e9f1c3b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "paper_trading_subscriptions"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("instrument_kind", sa.String(16), nullable=False, server_default="equity"),
    )
    op.add_column(_TABLE, sa.Column("contract_symbol", sa.String(48), nullable=True))
    op.add_column(_TABLE, sa.Column("contract_expiry", sa.Date(), nullable=True))
    op.add_column(_TABLE, sa.Column("lot_size", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("lots", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint(
        "ck_paper_trading_subscriptions_instrument_kind",
        _TABLE,
        "instrument_kind IN ('equity','future')",
    )
    op.create_check_constraint(
        "ck_paper_trading_subscriptions_future_has_contract",
        _TABLE,
        "instrument_kind = 'equity' OR (contract_symbol IS NOT NULL "
        "AND contract_expiry IS NOT NULL AND lot_size IS NOT NULL)",
    )


def downgrade() -> None:
    # A futures subscription cannot exist without its columns: remove those first (their fills and
    # positions are keyed by the subscription and go with it, as no other code path deletes them).
    op.execute(
        "DELETE FROM paper_fills WHERE subscription_id IN "
        f"(SELECT id FROM {_TABLE} WHERE instrument_kind = 'future')"
    )
    op.execute(
        "DELETE FROM paper_positions WHERE subscription_id IN "
        f"(SELECT id FROM {_TABLE} WHERE instrument_kind = 'future')"
    )
    op.execute(
        "DELETE FROM daily_signals WHERE subscription_id IN "
        f"(SELECT id FROM {_TABLE} WHERE instrument_kind = 'future')"
    )
    op.execute(f"DELETE FROM {_TABLE} WHERE instrument_kind = 'future'")
    op.drop_constraint("ck_paper_trading_subscriptions_future_has_contract", _TABLE, type_="check")
    op.drop_constraint("ck_paper_trading_subscriptions_instrument_kind", _TABLE, type_="check")
    for column in ("lots", "lot_size", "contract_expiry", "contract_symbol", "instrument_kind"):
        op.drop_column(_TABLE, column)
