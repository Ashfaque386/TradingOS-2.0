"""engine_alert_events: the history of what the engine needed a person for

One row per episode (opened when a condition is first seen, resolved when it clears), raised by
the in-app watcher, by an engine event (a stop-loss exit, a postponed roll, a refused broker
credential) or by Prometheus through Alertmanager. The console's alerts panel and the top-bar
count read it, so an alert is not lost when the notice that announced it scrolls away.

Revision ID: b4f7d2a8c1e3
Revises: a9e3c7f1d5b8
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4f7d2a8c1e3"
down_revision: str | None = "a9e3c7f1d5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "engine_alert_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "opened_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source IN ('watcher','event','alertmanager')", name="ck_engine_alert_events_source"
        ),
        sa.CheckConstraint(
            "severity IN ('critical','warning','info')", name="ck_engine_alert_events_severity"
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["paper_trading_subscriptions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_engine_alert_events_open_key",
        "engine_alert_events",
        ["key"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index("ix_engine_alert_events_opened_at", "engine_alert_events", ["opened_at"])


def downgrade() -> None:
    op.drop_index("ix_engine_alert_events_opened_at", table_name="engine_alert_events")
    op.drop_index("uq_engine_alert_events_open_key", table_name="engine_alert_events")
    op.drop_table("engine_alert_events")
