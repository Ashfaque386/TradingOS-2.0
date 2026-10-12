"""memory_settings

Agent long-term memory: the operator's embedding provider/model choice, kept
in the database instead of .env. Additive only -- a new singleton table,
nothing existing is touched (Non-Negotiable Rule #8).

Revision ID: b8e24f6a1c97
Revises: a3c91d7e5b42
Create Date: 2026-10-12 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8e24f6a1c97'
down_revision: Union[str, None] = 'a3c91d7e5b42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('memory_settings',
    sa.Column('id', sa.Integer(), autoincrement=False, nullable=False),
    sa.Column('embedding_provider', sa.String(length=32), nullable=False),
    sa.Column('embedding_model', sa.String(length=200), nullable=False),
    sa.Column('updated_by', sa.String(length=320), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('id = 1', name='ck_memory_settings_singleton'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('memory_settings')
