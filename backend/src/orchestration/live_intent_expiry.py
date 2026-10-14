"""The live-intent expiry sweep (Build Spec §12.2).

Lives in its own module so the mutation gate (`scripts/mutation_gate.py`) can mutate
just these few lines: it used to sit inside `live_trading.py`, an 800-line file whose
other mutants would have buried it, and was covered only by a hand-driven analysis.
`src.orchestration.live_trading` re-exports it, so every existing import still works.
"""

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.live_order_intent import LiveOrderIntent

__all__ = ["SWEPT_STATUSES", "expire_stale_intents"]

# The unsubmitted statuses the sweep resolves. Anything else (submitted, capped, failed,
# expired...) is already resolved and is never touched.
SWEPT_STATUSES = ("pending_approval", "approved", "generated")


async def expire_stale_intents(db: AsyncSession) -> int:
    """The scheduled sweep (Build Spec §12.2): every unsubmitted intent
    past its `expires_at` resolves to `expired` -- safely, with no order
    ever submitted. Since Phase 18, the only new-vocabulary status this
    can ever actually catch is `generated` (the rare "no broker adapter
    configured at generation time" case -- see
    `generate_live_order_intent`'s docstring); `pending_approval`/
    `approved` stay in the filter only so a historical pre-Phase-18 row
    stuck in either state still gets swept correctly, not because new
    code ever produces them. This must be correct even if nobody is
    watching the console at all, which is exactly why this is a DB-level
    bulk UPDATE driven by `src.orchestration.live_trading_scheduler`'s
    own timer, never a client-side countdown that silently does nothing
    if the browser tab is closed."""
    now = datetime.now(UTC)
    stmt = (
        update(LiveOrderIntent)
        .where(
            LiveOrderIntent.status.in_(SWEPT_STATUSES),
            LiveOrderIntent.expires_at <= now,
        )
        .values(status="expired")
    )
    result = await db.execute(stmt)
    await db.commit()
    # An UPDATE statement's execute() always returns a real CursorResult at
    # runtime (.rowcount is populated) -- Result[Any] is only the generic
    # base type db.execute()'s signature can express statically.
    return cast(CursorResult[Any], result).rowcount
