"""run_cancel_statuses

Cancelling a run (backlog item 21): a `cancelled` label on the two native
enums, `run_status` and `task_status`. Additive only -- no table or column is
touched (Non-Negotiable Rule #8).

`ALTER TYPE ... ADD VALUE` is allowed inside a transaction block on
Postgres 12+ (this project runs 16); the one restriction is that a new label
cannot be *used* until the transaction that added it commits, and this
migration only adds it. `IF NOT EXISTS` keeps an upgrade -> downgrade ->
upgrade cycle idempotent.

Postgres cannot drop an enum label, so the downgrade leaves the two (then
unused) labels in place and instead moves the data back onto the old
vocabulary: a cancelled run becomes a permanently failed one and a cancelled
task an unsatisfiable one -- the nearest meanings the previous code
understands -- so nothing is left holding a status the old code cannot load.

Revision ID: c4a7e1b95d03
Revises: b8e24f6a1c97
Create Date: 2026-10-12 17:20:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c4a7e1b95d03'
down_revision: Union[str, None] = 'b8e24f6a1c97'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TYPE run_status ADD VALUE IF NOT EXISTS 'cancelled'")
    op.execute("ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'cancelled'")


def downgrade() -> None:
    op.execute(
        "UPDATE organization_runs "
        "SET status = 'failed', failure_class = 'permanent', "
        "error = COALESCE(error, 'cancelled (reverted to failed by a migration downgrade)') "
        "WHERE status = 'cancelled'"
    )
    op.execute("UPDATE tasks SET status = 'unsatisfiable' WHERE status = 'cancelled'")
