"""two more market-data pipelines in the provenance check: history backfill and gap repair

Fetching a symbol's real price history (when a subscription is enrolled, and the 07:30 IST repair
before the morning's signals) is recorded in `market_data_provenance` like every other pipeline;
the CHECK constraint on `pipeline` lists the allowed names, so it gains the two new ones.

Revision ID: f6c3d8e0b2a4
Revises: e5b2c7d9a1f3
Create Date: 2026-10-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f6c3d8e0b2a4"
down_revision: str | None = "e5b2c7d9a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "'incremental_daily','corporate_actions','instrument_master',"
    "'intraday_minute','bhavcopy_fallback','catalog_refresh','data_lake_backup'"
)
_NEW = _OLD + ",'history_backfill','history_gap_repair'"
_NAME = "ck_market_data_provenance_pipeline"
_TABLE = "market_data_provenance"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, f"pipeline IN ({_NEW})")


def downgrade() -> None:
    # Rows written by the new pipelines would violate the old constraint: remove them first.
    op.execute(
        f"DELETE FROM {_TABLE} WHERE pipeline IN ('history_backfill','history_gap_repair')"
    )
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, f"pipeline IN ({_OLD})")
