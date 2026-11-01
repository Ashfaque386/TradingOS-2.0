"""users: the state of a failing sign-in, per account

A wrong password is counted against the account it was tried on, so an attacker who rotates
addresses still meets a lock. `failed_login_attempts` counts failures since the last success (or the
last lock), `last_failed_login_at` lets old failures age out, and `locked_until` is when a lock ends.
All three are additive; an existing account starts with no failures and no lock.

Revision ID: c5a1e9f3b7d4
Revises: b4f7d2a8c1e3
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5a1e9f3b7d4"
down_revision: str | None = "b4f7d2a8c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("failed_login_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "users", sa.Column("last_failed_login_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "locked_until")
    op.drop_column("users", "last_failed_login_at")
    op.drop_column("users", "failed_login_attempts")
