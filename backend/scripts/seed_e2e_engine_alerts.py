"""E2E test-fixture seeding for the engine alerts panel (e2e/engine-alerts.spec.ts).

The alerts are raised by a watcher that looks at live subscriptions, by events inside the trading
engine (a stop-loss exit) and by Prometheus through Alertmanager. None of that can be made to
happen on cue in a browser test, so this writes the episodes those paths would have left, the way
`src.orchestration.engine_alerts` records them:

- a paper subscription that is switched off and still holds shares, so an alert can point at
  something on the desk and the real watcher, which runs in the backend under test, agrees with the
  alert below instead of clearing it a minute later;
- an open critical alert from the watcher about that subscription (a position nobody is watching),
  already announced;
- an open warning that came through Alertmanager (PaperTicksStale), not tied to a subscription;
- a warning from the watcher that cleared before it was ever announced;
- a stop-loss exit event, which opens and resolves in the same instant.

Each run uses its own symbol and keys, so the spec can run repeatedly against one database.

Usage:
    python scripts/seed_e2e_engine_alerts.py
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from src.core.db import get_session_factory
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.engine_alert_event import EngineAlertEvent
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import run_strategy_pipeline


async def seed() -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    symbol = f"E2EAL{suffix}"
    async with session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name=f"E2E Alerts {suffix}",
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    now = datetime.now(UTC)
    async with session_factory() as db:
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id,
            symbol=symbol,
            initial_capital=100_000.0,
            created_by="e2e-seed-script",
            is_active=False,
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperPosition(
                subscription_id=subscription.id, symbol=symbol, quantity=10, avg_cost=100.0
            )
        )
        db.add_all(
            [
                EngineAlertEvent(
                    key=f"{subscription.id}:unmanaged",
                    source="watcher",
                    kind="unmanaged",
                    severity="critical",
                    title=f"{symbol} is holding a position nobody is watching",
                    detail="The subscription is switched off, so its stop-loss no longer runs.",
                    subscription_id=subscription.id,
                    opened_at=now - timedelta(minutes=12),
                    last_seen_at=now,
                    notified_at=now - timedelta(minutes=12),
                ),
                EngineAlertEvent(
                    key=f"alertmanager:e2e-{suffix}",
                    source="alertmanager",
                    kind="PaperTicksStale",
                    severity="warning",
                    title=f"Paper-trading ticks are arriving too old to act on ({suffix})",
                    detail="More than 3 ticks in 10 minutes were older than the limit.",
                    opened_at=now - timedelta(minutes=5),
                    last_seen_at=now,
                    notified_at=now - timedelta(minutes=5),
                ),
                EngineAlertEvent(
                    key=f"{subscription.id}:halted",
                    source="watcher",
                    kind="halted",
                    severity="warning",
                    title=f"{symbol} is not trading",
                    detail="There is no real price feed for it.",
                    subscription_id=subscription.id,
                    opened_at=now - timedelta(minutes=40),
                    last_seen_at=now - timedelta(minutes=39),
                    resolved_at=now - timedelta(minutes=39),
                ),
                EngineAlertEvent(
                    key=f"stop_loss:e2e-{suffix}",
                    source="event",
                    kind="stop_loss",
                    severity="warning",
                    title=f"{symbol}: stop-loss hit, position closed",
                    detail="Sold 10 of 10 at an average 97.00 (cost 100.00, stop-loss 3%).",
                    subscription_id=subscription.id,
                    opened_at=now - timedelta(minutes=20),
                    last_seen_at=now - timedelta(minutes=20),
                    resolved_at=now - timedelta(minutes=20),
                    notified_at=now - timedelta(minutes=20),
                ),
            ]
        )
        await db.commit()

    print(f"symbol={symbol}")
    print(f"suffix={suffix}")


if __name__ == "__main__":
    asyncio.run(seed())
