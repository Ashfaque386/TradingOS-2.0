"""trade reconciliation fields

Revision ID: 5fb9d5f828c6
Revises: e366a7d9d374
Create Date: 2026-09-22 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5fb9d5f828c6'
down_revision: Union[str, None] = 'e366a7d9d374'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('trades', sa.Column('fill_price', sa.Float(), nullable=True))
    op.add_column('trades', sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint('ck_trades_status', 'trades', type_='check')
    op.create_check_constraint(
        'ck_trades_status',
        'trades',
        "status IN ('pending_confirmation','filled','rejected','cancelled','failed')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_trades_status', 'trades', type_='check')
    op.create_check_constraint(
        'ck_trades_status',
        'trades',
        "status IN ('pending_confirmation','failed')",
    )
    op.drop_column('trades', 'confirmed_at')
    op.drop_column('trades', 'fill_price')
