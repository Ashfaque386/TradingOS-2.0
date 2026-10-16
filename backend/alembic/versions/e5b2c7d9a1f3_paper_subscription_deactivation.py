"""who deactivated a paper-trading subscription, when and why

`is_active` was only ever set at enrolment; stopping a subscription meant editing the database
and the only trace was whatever audit row someone remembered to write. The deactivate/activate
controls record the person, the time and the reason on the row itself. Subscriptions that were
already inactive get the three fields back-filled from their `paper_trading.subscription_deactivated`
audit entry when there is one (the column stays NULL when nothing records it).

Revision ID: e5b2c7d9a1f3
Revises: d1f5a8c3b7e2
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b2c7d9a1f3"
down_revision: str | None = "d1f5a8c3b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_trading_subscriptions",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "paper_trading_subscriptions", sa.Column("deactivated_by", sa.String(128), nullable=True)
    )
    op.add_column(
        "paper_trading_subscriptions", sa.Column("deactivation_reason", sa.Text(), nullable=True)
    )
    # Back-fill from the audit trail where it exists (the audit table is the only record of who).
    op.execute(
        """
        UPDATE paper_trading_subscriptions AS s
           SET deactivated_at = a.created_at,
               deactivated_by = a.actor,
               deactivation_reason = a.details ->> 'reason'
          FROM audit_log AS a
         WHERE s.is_active = false
           AND a.action = 'paper_trading.subscription_deactivated'
           AND a.entity_id = s.id::text
        """
    )


def downgrade() -> None:
    op.drop_column("paper_trading_subscriptions", "deactivation_reason")
    op.drop_column("paper_trading_subscriptions", "deactivated_by")
    op.drop_column("paper_trading_subscriptions", "deactivated_at")
