"""live trading autonomy (Phase 18: remove per-order human approval)

Revision ID: e9764af6c00d
Revises: 5fb9d5f828c6
Create Date: 2026-09-23 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e9764af6c00d'
down_revision: Union[str, None] = '5fb9d5f828c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'live_trading_subscriptions',
        sa.Column(
            'autonomous_trading_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        'live_trading_subscriptions',
        sa.Column('autonomy_enabled_by', sa.String(length=128), nullable=True),
    )
    op.add_column(
        'live_trading_subscriptions',
        sa.Column('autonomy_enabled_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'live_trading_subscriptions',
        sa.Column('max_intents_per_window', sa.Integer(), nullable=False, server_default='5'),
    )
    op.add_column(
        'live_trading_subscriptions',
        sa.Column(
            'rate_limit_window_minutes', sa.Integer(), nullable=False, server_default='60'
        ),
    )
    op.add_column(
        'live_trading_subscriptions',
        sa.Column(
            'max_notional_per_intent', sa.Float(), nullable=False, server_default='50000.0'
        ),
    )
    op.create_check_constraint(
        'ck_live_trading_subscriptions_max_intents_per_window',
        'live_trading_subscriptions',
        'max_intents_per_window > 0',
    )
    op.create_check_constraint(
        'ck_live_trading_subscriptions_rate_limit_window_minutes',
        'live_trading_subscriptions',
        'rate_limit_window_minutes > 0',
    )
    op.create_check_constraint(
        'ck_live_trading_subscriptions_max_notional_per_intent',
        'live_trading_subscriptions',
        'max_notional_per_intent > 0',
    )

    # Additive union, not a replacement: 'pending_approval'/'approved'/
    # 'rejected' stay valid so any pre-existing production rows in those
    # states don't fail this constraint's validation; new code never
    # writes them again (see LiveOrderIntent's own docstring).
    op.drop_constraint('ck_live_order_intents_status', 'live_order_intents', type_='check')
    op.create_check_constraint(
        'ck_live_order_intents_status',
        'live_order_intents',
        "status IN ('pending_approval','approved','rejected','expired',"
        "'submitted','failed','generated','capped')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_live_order_intents_status', 'live_order_intents', type_='check')
    op.create_check_constraint(
        'ck_live_order_intents_status',
        'live_order_intents',
        "status IN ('pending_approval','approved','rejected','expired','submitted','failed')",
    )

    op.drop_constraint(
        'ck_live_trading_subscriptions_max_notional_per_intent',
        'live_trading_subscriptions',
        type_='check',
    )
    op.drop_constraint(
        'ck_live_trading_subscriptions_rate_limit_window_minutes',
        'live_trading_subscriptions',
        type_='check',
    )
    op.drop_constraint(
        'ck_live_trading_subscriptions_max_intents_per_window',
        'live_trading_subscriptions',
        type_='check',
    )
    op.drop_column('live_trading_subscriptions', 'max_notional_per_intent')
    op.drop_column('live_trading_subscriptions', 'rate_limit_window_minutes')
    op.drop_column('live_trading_subscriptions', 'max_intents_per_window')
    op.drop_column('live_trading_subscriptions', 'autonomy_enabled_at')
    op.drop_column('live_trading_subscriptions', 'autonomy_enabled_by')
    op.drop_column('live_trading_subscriptions', 'autonomous_trading_enabled')
