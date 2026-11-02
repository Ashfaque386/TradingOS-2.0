"""E2E test-fixture seeding: a live position for a strategy.

e2e/strategies-review.spec.ts checks that a live subscription holding shares cannot be switched off
and that its order cap cannot go below what it holds. A real position would need a real order and a
broker, so the row a filled order would have left is written directly.

Usage:
    python scripts/seed_e2e_live_position.py <strategy_id> <symbol> <quantity> <avg_cost>
"""

import asyncio
import sys
import uuid

from src.core.db import get_session_factory
from src.models.live_position import LivePosition


async def seed(strategy_id: uuid.UUID, symbol: str, quantity: int, avg_cost: float) -> None:
    session_factory = get_session_factory()
    async with session_factory() as db:
        db.add(
            LivePosition(
                strategy_id=strategy_id, symbol=symbol, quantity=quantity, avg_cost=avg_cost
            )
        )
        await db.commit()
    print(f"quantity={quantity}")
    print(f"cost={abs(quantity) * avg_cost}")


if __name__ == "__main__":
    asyncio.run(seed(uuid.UUID(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4])))
