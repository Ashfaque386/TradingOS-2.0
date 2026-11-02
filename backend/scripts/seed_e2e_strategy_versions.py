"""E2E test-fixture seeding: a strategy with two versions and two suggestions.

e2e/strategies-review.spec.ts reads a strategy's versions, the difference between them and the
suggestions made for it. Getting there through the page would mean the AI generator and reviewer,
so the rows are written directly: version 2 is what the first suggestion was regenerated into, and
the second suggestion is still waiting. The code of the two versions differs by three added lines
and two removed ones, which the spec checks. The name is new each run.

Usage:
    python scripts/seed_e2e_strategy_versions.py
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from src.core.db import get_session_factory
from src.models.strategy_suggestion import StrategySuggestion
from src.orchestration.strategies import create_strategy, create_version_with_validation

CODE_V1 = 'def run_backtest(data, config):\n    stop = 5\n    return {"stop": stop}\n'
CODE_V2 = (
    "def run_backtest(data, config):\n"
    "    stop = 3\n"
    "    size = 10\n"
    '    return {"stop": stop, "size": size}\n'
)


async def seed() -> None:
    session_factory = get_session_factory()
    name = f"E2E Versions {uuid.uuid4().hex[:6]}"
    now = datetime.now(UTC)
    async with session_factory() as db:
        strategy = await create_strategy(db, name=name, objective="Buy on a bullish crossover")
        first = await create_version_with_validation(db, strategy, CODE_V1)
        second = await create_version_with_validation(db, strategy, CODE_V2)
        db.add_all(
            [
                StrategySuggestion(
                    strategy_id=strategy.id,
                    base_version_id=first.id,
                    suggestion_text="Tighten the stop and size the entry",
                    requested_by="e2e.pm@example.com",
                    status="regenerated",
                    ai_verdict={"verdict": "accept", "notes": "A tighter stop is sensible."},
                    regenerated_version_id=second.id,
                    regeneration_diff="- stop = 5\n+ stop = 3\n+ size = 10",
                    created_at=now - timedelta(days=2),
                    reviewed_at=now - timedelta(days=2),
                ),
                StrategySuggestion(
                    strategy_id=strategy.id,
                    base_version_id=second.id,
                    suggestion_text="Skip the entry on a gap larger than two percent",
                    requested_by="e2e.admin@example.com",
                    status="pending",
                    created_at=now - timedelta(hours=1),
                ),
            ]
        )
        await db.commit()
        strategy_id = strategy.id

    print(f"strategy_id={strategy_id}")
    print(f"name={name}")


if __name__ == "__main__":
    asyncio.run(seed())
