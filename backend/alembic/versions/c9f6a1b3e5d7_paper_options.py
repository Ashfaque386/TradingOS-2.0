"""paper subscriptions can trade options: legs as positions, a strategy, wider symbols

A subscription held at most one position (one row, unique on the subscription). An option strategy
holds one or two legs, each its own contract, so a position is now unique on (subscription,
symbol) -- the existing equity and futures subscriptions still have exactly one. An option contract's
name is longer than an equity symbol, so the symbol columns on positions and fills are widened.
`instrument_kind` gains `option`, with the strategy it runs and, for a vertical spread, the width.

Revision ID: c9f6a1b3e5d7
Revises: b8e5f0a2d4c6
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9f6a1b3e5d7"
down_revision: str | None = "b8e5f0a2d4c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SUBS = "paper_trading_subscriptions"
_KIND = "ck_paper_trading_subscriptions_instrument_kind"
_FUTURE = "ck_paper_trading_subscriptions_future_has_contract"
_OPTION = "ck_paper_trading_subscriptions_option_has_strategy"


def upgrade() -> None:
    op.drop_constraint("uq_paper_positions_subscription_id", "paper_positions", type_="unique")
    op.create_unique_constraint(
        "uq_paper_positions_subscription_symbol", "paper_positions", ["subscription_id", "symbol"]
    )
    for table in ("paper_positions", "paper_fills"):
        op.alter_column(
            table, "symbol", type_=sa.String(48), existing_type=sa.String(32), existing_nullable=False
        )
    op.add_column(_SUBS, sa.Column("option_strategy", sa.String(32), nullable=True))
    op.add_column(_SUBS, sa.Column("spread_width", sa.Integer(), nullable=False, server_default="2"))
    op.drop_constraint(_FUTURE, _SUBS, type_="check")
    op.drop_constraint(_KIND, _SUBS, type_="check")
    op.create_check_constraint(_KIND, _SUBS, "instrument_kind IN ('equity','future','option')")
    op.create_check_constraint(
        _FUTURE,
        _SUBS,
        "instrument_kind <> 'future' OR (contract_symbol IS NOT NULL "
        "AND contract_expiry IS NOT NULL AND lot_size IS NOT NULL)",
    )
    op.create_check_constraint(
        _OPTION,
        _SUBS,
        "instrument_kind <> 'option' OR option_strategy IN ('long_option','vertical_spread')",
    )


def downgrade() -> None:
    # Option subscriptions cannot exist without their columns and kind, and a subscription with
    # several legs cannot be one position: remove them, and what hangs off them, first.
    for table in ("paper_fills", "paper_positions", "daily_signals"):
        op.execute(
            f"DELETE FROM {table} WHERE subscription_id IN "
            f"(SELECT id FROM {_SUBS} WHERE instrument_kind = 'option')"
        )
    op.execute(f"DELETE FROM {_SUBS} WHERE instrument_kind = 'option'")
    op.drop_constraint(_OPTION, _SUBS, type_="check")
    op.drop_constraint(_FUTURE, _SUBS, type_="check")
    op.drop_constraint(_KIND, _SUBS, type_="check")
    op.create_check_constraint(_KIND, _SUBS, "instrument_kind IN ('equity','future')")
    op.create_check_constraint(
        _FUTURE,
        _SUBS,
        "instrument_kind = 'equity' OR (contract_symbol IS NOT NULL "
        "AND contract_expiry IS NOT NULL AND lot_size IS NOT NULL)",
    )
    op.drop_column(_SUBS, "spread_width")
    op.drop_column(_SUBS, "option_strategy")
    for table in ("paper_fills", "paper_positions"):
        op.alter_column(table, "symbol", type_=sa.String(32), existing_type=sa.String(48), existing_nullable=False)
    op.drop_constraint("uq_paper_positions_subscription_symbol", "paper_positions", type_="unique")
    op.create_unique_constraint(
        "uq_paper_positions_subscription_id", "paper_positions", ["subscription_id"]
    )
