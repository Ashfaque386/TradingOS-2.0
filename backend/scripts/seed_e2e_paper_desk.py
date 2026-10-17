"""E2E test-fixture seeding for the paper-trading desk (e2e/paper-trading-desk.spec.ts).

The desk shows what the paper engine has done: positions, fills, their costs, a subscription that
nobody is watching. Reaching those states through the engine would need real broker ticks and
depth (the whole point of the engine is that it refuses to invent them), so this script writes the
resulting rows directly, the way an earlier run of the engine would have left them:

- an active equity subscription holding 10 shares, with a fill that was charged costs;
- a switched-off subscription still holding 5 shares -- the "nobody is watching" case;
- an active bull-put credit spread: a sold 24500 PE and the bought 24400 PE that hedges it, both
  legs open, two months from expiry.

Prices are not seeded (there is no Redis tick), so every open leg is unpriced -- which the desk
must say, not show as zero. Each run uses its own strategy and fresh symbols, so the spec can run
repeatedly against one database.

Usage:
    python scripts/seed_e2e_paper_desk.py
"""

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta

from src.brokers.base import option_symbol
from src.core.db import get_session_factory
from src.engine.sandbox.process_runtime import RestrictedProcessSandboxRuntime
from src.models.data_source import DataSource
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.strategies import run_strategy_pipeline


async def seed() -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    strategy_name = f"E2E Paper Desk {suffix}"
    async with session_factory() as db:
        _strategy, version = await run_strategy_pipeline(
            db,
            name=strategy_name,
            objective="obj",
            sandbox_runtime=RestrictedProcessSandboxRuntime(),
        )
    version_id = version.id

    held = f"E2EHELD{suffix}"
    loose = f"E2ELOOSE{suffix}"
    underlying = f"E2ESP{suffix}"
    expiry = date.today() + timedelta(days=60)
    sold_leg = option_symbol(underlying, expiry, 24500, "PE")
    bought_leg = option_symbol(underlying, expiry, 24400, "PE")

    async with session_factory() as db:
        held_sub = PaperTradingSubscription(
            strategy_version_id=version_id, symbol=held, created_by="e2e-seed-script"
        )
        loose_sub = PaperTradingSubscription(
            strategy_version_id=version_id,
            symbol=loose,
            is_active=False,
            deactivated_at=datetime.now(UTC),
            deactivated_by="e2e-seed-script",
            deactivation_reason="left open on purpose",
            created_by="e2e-seed-script",
        )
        spread_sub = PaperTradingSubscription(
            strategy_version_id=version_id,
            symbol=underlying,
            instrument_kind="option",
            option_strategy="vertical_spread",
            lots=1,
            lot_size=50,
            stop_loss_pct=50.0,
            created_by="e2e-seed-script",
        )
        db.add_all([held_sub, loose_sub, spread_sub])
        await db.flush()

        db.add_all(
            [
                PaperPosition(
                    subscription_id=held_sub.id,
                    symbol=held,
                    quantity=10,
                    avg_cost=100.0,
                    realized_pnl=500.0,
                    costs_total=40.0,
                ),
                PaperPosition(
                    subscription_id=loose_sub.id, symbol=loose, quantity=5, avg_cost=200.0
                ),
                PaperPosition(
                    subscription_id=spread_sub.id, symbol=sold_leg, quantity=-50, avg_cost=150.0
                ),
                PaperPosition(
                    subscription_id=spread_sub.id, symbol=bought_leg, quantity=50, avg_cost=100.0
                ),
                PaperFill(
                    subscription_id=held_sub.id,
                    symbol=held,
                    side="buy",
                    data_source=DataSource.REAL,
                    order_group_id=uuid.uuid4(),
                    leg_index=0,
                    requested_quantity=10,
                    filled_quantity=10,
                    avg_fill_price=100.0,
                    fully_filled=True,
                    costs_total=40.0,
                    costs_breakdown={"brokerage": 20.0, "stt": 12.5, "gst": 7.5},
                ),
            ]
        )
        spread_order = uuid.uuid4()
        for index, (leg, side, price, costs) in enumerate(
            [(sold_leg, "sell", 150.0, 33.4), (bought_leg, "buy", 100.0, 28.1)]
        ):
            db.add(
                PaperFill(
                    subscription_id=spread_sub.id,
                    symbol=leg,
                    side=side,
                    data_source=DataSource.REAL,
                    order_group_id=spread_order,
                    leg_index=index,
                    requested_quantity=50,
                    filled_quantity=50,
                    avg_fill_price=price,
                    fully_filled=True,
                    costs_total=costs,
                    costs_breakdown={"brokerage": 20.0, "gst": round(costs - 20.0, 2)},
                )
            )
        await db.commit()

    print(f"strategy_name={strategy_name}")
    print(f"held={held}")
    print(f"loose={loose}")
    print(f"underlying={underlying}")


if __name__ == "__main__":
    asyncio.run(seed())
