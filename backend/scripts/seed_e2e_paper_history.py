"""E2E test-fixture seeding: a paper subscription with a history of orders and signals.

e2e/paper-desk-history.spec.ts needs a lane with more orders than the desk shows at first (one of
them a manual close) and two daily signals (one acted on, one not). The rows are written directly,
the way an earlier run of the engine would have left them, with fixed times so their order is the
same every run. The subscription's symbol is new each run, and the subscription is switched off:
while the market is open the engine trades every active subscription, which would add orders and
signals of its own to the ones the spec counts. (The state "waiting for price history" cannot be
reached here at all: with the synthetic feed the history is generated, so the spec states it.)

Usage:
    python scripts/seed_e2e_paper_history.py
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from src.core.db import get_session_factory
from src.models.daily_signal import DailySignal
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
ORDERS = 12
MANUAL_CLOSE_INDEX = 5


async def seed() -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    name = f"E2E Paper History {suffix}"
    symbol = f"E2EHIST{suffix}"
    base = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)

    async with session_factory() as db:
        strategy = await create_strategy(db, name=name, objective="obj")
        version = await create_version_with_validation(db, strategy, _CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id,
            symbol=symbol,
            created_by="e2e-seed-script",
            is_active=False,
        )
        db.add(subscription)
        await db.flush()

        for i in range(ORDERS):
            db.add(
                PaperFill(
                    subscription_id=subscription.id,
                    symbol=symbol,
                    side="buy" if i % 2 == 0 else "sell",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=10,
                    filled_quantity=10,
                    avg_fill_price=100.0 + i,
                    fully_filled=True,
                    data_source="real",
                    realized_pnl=0.0 if i % 2 == 0 else 25.0,
                    costs_total=5.0,
                    costs_breakdown={"brokerage": 4.0, "stt": 1.0},
                    administrative=i == MANUAL_CLOSE_INDEX,
                    created_at=base + timedelta(minutes=i),
                )
            )
        db.add_all(
            [
                DailySignal(
                    subscription_id=subscription.id,
                    symbol=symbol,
                    signal_type="BUY",
                    reference_price=99.5,
                    consumed=True,
                    consumed_at=base + timedelta(minutes=1),
                    generated_at=base - timedelta(days=1),
                ),
                DailySignal(
                    subscription_id=subscription.id,
                    symbol=symbol,
                    signal_type="SELL",
                    reference_price=111.0,
                    generated_at=base,
                ),
            ]
        )
        await db.commit()

    print(f"name={name}")
    print(f"symbol={symbol}")
    print(f"orders={ORDERS}")


if __name__ == "__main__":
    asyncio.run(seed())
