"""E2E test-fixture seeding for the automatic kill switch (e2e/kill-switch-auto.spec.ts).

The monitor trips the switch when measured equity has fallen far enough below its highest recorded
value. Reaching that through the engine would need a real losing run, so this writes the starting
state directly:

- a paper subscription with 100,000 of capital, so there is something to measure against;
- a latest tick for every open paper position, at its own average cost, so none is unpriced (other
  specs leave priced-by-nobody positions behind, and an unpriced position makes the equity unknown,
  which is the right thing for the monitor to say and the wrong thing for this spec to wait on);
- one earlier reading with an equity of `--peak` (default 50,000,000), taken just now: a reading from
  before the last human reset would be ignored, since a reset re-bases the peak.

Measured equity is then a tiny fraction of that peak, so the next evaluation trips the switch. Each
run uses its own strategy and symbol, so the spec can run repeatedly against one database.

Usage:
    python scripts/seed_e2e_equity.py [--peak 50000000]
"""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from src.core.db import get_session_factory
from src.core.redis_client import get_redis
from src.engine.paper_trading.tick_feed import Tick, publish_tick
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.equity_snapshot import EquitySnapshot
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import run_strategy_pipeline


async def seed(peak: float) -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    async with session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name=f"E2E Equity {suffix}",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    async with session_factory() as db:
        db.add(
            PaperTradingSubscription(
                strategy_version_id=version.id,
                symbol=f"E2EEQ{suffix}",
                initial_capital=100_000.0,
                created_by="e2e-seed-script",
            )
        )
        open_positions = (
            (await db.execute(select(PaperPosition).where(PaperPosition.quantity != 0)))
            .scalars()
            .all()
        )
        db.add(
            EquitySnapshot(
                mode="paper",
                taken_at=datetime.now(UTC),
                equity=peak,
                capital_base=peak,
                realized_net=0.0,
                unrealized=0.0,
                open_positions=0,
                unpriced_positions=0,
                price_source="real",
                peak_equity=peak,
                drawdown_pct=0.0,
                note="seeded by scripts/seed_e2e_equity.py",
            )
        )
        await db.commit()

    redis = get_redis()
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    for position in open_positions:
        await publish_tick(
            redis,
            Tick(
                symbol=position.symbol, price=position.avg_cost, timestamp_ms=now_ms, source="real"
            ),
        )
    await redis.aclose()
    print(f"peak={peak}")
    print(f"priced_positions={len(open_positions)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--peak", type=float, default=50_000_000.0)
    asyncio.run(seed(parser.parse_args().peak))
