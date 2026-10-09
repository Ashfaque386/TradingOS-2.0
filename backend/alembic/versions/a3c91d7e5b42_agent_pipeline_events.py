"""agent_pipeline_events

Phase 19 Finding #1 (live per-step agent feed): one row per step of one
LangGraph pipeline run. Additive only -- a new table, nothing existing is
touched (Non-Negotiable Rule #8).

Revision ID: a3c91d7e5b42
Revises: c7f60e9797b1
Create Date: 2026-10-09 22:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3c91d7e5b42'
down_revision: Union[str, None] = 'c7f60e9797b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agent_pipeline_events',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('event_type', sa.String(length=64), nullable=False),
    sa.Column('node', sa.String(length=64), nullable=True),
    sa.Column('agent_id', sa.String(length=64), nullable=True),
    sa.Column('payload', sa.JSON(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'sequence')
    )
    op.create_index(op.f('ix_agent_pipeline_events_run_id'), 'agent_pipeline_events', ['run_id'], unique=False)
    op.create_index('ix_agent_pipeline_events_created_at', 'agent_pipeline_events', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_agent_pipeline_events_created_at', table_name='agent_pipeline_events')
    op.drop_index(op.f('ix_agent_pipeline_events_run_id'), table_name='agent_pipeline_events')
    op.drop_table('agent_pipeline_events')
