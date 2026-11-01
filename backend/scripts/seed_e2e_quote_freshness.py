"""E2E test-fixture seeding for the price-freshness section of the Paper Desk lanes
(e2e/quote-freshness.spec.ts).

What a lane says about its latest price depends on what the broker's quote carried, and a broker
quote cannot be made to carry it on cue. So this writes the ticks (and one refusal) the tick source
would have left in Redis, for four active subscriptions of one strategy:

- fresh:     stamped by the exchange seconds ago, with circuit limits 90 to 110;
- quiet:     stamped fifteen minutes ago, no limits (a quiet instrument, or a feed that stopped);
- unstamped: no exchange stamp at all (the broker sends none we can read);
- refused:   a tick, and a standing refusal: its quote's stamp was from a previous day.

Each run has its own strategy and symbols, so the spec can run repeatedly against one database. The
refusal is stored for ten minutes, not the engine's thirty seconds, so a slow run still sees it.

Usage:
    python scripts/seed_e2e_quote_freshness.py
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

from src.core.db import get_session_factory
from src.core.redis_client import get_redis
from src.engine.paper_trading.tick_feed import Tick, publish_tick, quote_refusal_key
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import run_strategy_pipeline

STAMP_FROM_A_PREVIOUS_DAY = "the exchange stamp (2026-10-30 15:29 IST) is from a previous day"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


async def seed() -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    symbols = {
        "fresh": f"E2EFR{suffix}",
        "quiet": f"E2EQT{suffix}",
        "unstamped": f"E2ENS{suffix}",
        "refused": f"E2ERF{suffix}",
    }
    async with session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name=f"E2E Freshness {suffix}",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    async with session_factory() as db:
        for symbol in symbols.values():
            db.add(
                PaperTradingSubscription(
                    strategy_version_id=version.id,
                    symbol=symbol,
                    initial_capital=100_000.0,
                    created_by="e2e-seed-script",
                )
            )
        await db.commit()

    now = datetime.now(UTC)
    redis = get_redis()
    await publish_tick(
        redis,
        Tick(
            symbols["fresh"],
            101.5,
            _ms(now),
            "real",
            exchange_time_ms=_ms(now - timedelta(seconds=5)),
            lower_circuit=90.0,
            upper_circuit=110.0,
        ),
    )
    await publish_tick(
        redis,
        Tick(
            symbols["quiet"],
            250.0,
            _ms(now),
            "real",
            exchange_time_ms=_ms(now - timedelta(minutes=15)),
        ),
    )
    await publish_tick(redis, Tick(symbols["unstamped"], 75.0, _ms(now), "real"))
    await publish_tick(
        redis,
        Tick(
            symbols["refused"],
            300.0,
            _ms(now),
            "real",
            exchange_time_ms=_ms(now - timedelta(days=1)),
        ),
    )
    await redis.set(
        quote_refusal_key(symbols["refused"]),
        json.dumps(
            {
                "broker": "zerodha",
                "reason": STAMP_FROM_A_PREVIOUS_DAY,
                "exchange_time": (now - timedelta(days=1)).isoformat(),
                "at": now.isoformat(),
            }
        ),
        ex=600,
    )
    await redis.aclose()

    for kind, symbol in symbols.items():
        print(f"{kind}={symbol}")


if __name__ == "__main__":
    asyncio.run(seed())
