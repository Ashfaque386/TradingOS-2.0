"""E2E test-fixture seeding: a strategy with a history on the Orders & Trades page.

e2e/orders.spec.ts needs executions to page through, filter and open: simulated and real paper
fills (one a manual close, one partial), a live order a broker accepted with its trades, a live
order that failed with a reason, open positions (one on a switched-off subscription that nothing is
watching), and live order intents. Reaching that through the UI alone would mean running the whole
strategy lifecycle and a day of trading, so the rows are written directly, with fixed times so the
order is the same every run. Every name and symbol is new each run, so a spec filters on them and
earlier runs do not get in the way.

Usage:
    python scripts/seed_e2e_orders.py
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from src.core.db import get_session_factory
from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.trade import Trade
from src.orchestration.strategies import create_strategy, create_version_with_validation

_CODE = "def run_backtest(data, config):\n    return {}\n"
PAPER_FILLS = 30


async def seed() -> None:
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6]
    name = f"E2E Orders {suffix}"
    symbol = f"ORD{suffix}".upper()
    live_symbol = f"LIV{suffix}".upper()
    base = datetime(2026, 11, 2, 9, 30, tzinfo=UTC)
    reason = "Insufficient funds for this order"

    async with session_factory() as db:
        strategy = await create_strategy(db, name=name, objective="obj")
        version = await create_version_with_validation(db, strategy, _CODE)

        watched = PaperTradingSubscription(
            strategy_version_id=version.id, symbol=symbol, builtin_strategy="always_long"
        )
        unwatched = PaperTradingSubscription(
            strategy_version_id=version.id,
            symbol=f"{symbol}X",
            builtin_strategy="always_long",
            is_active=False,
        )
        db.add_all([watched, unwatched])
        await db.flush()

        for i in range(PAPER_FILLS):
            db.add(
                PaperFill(
                    subscription_id=watched.id,
                    symbol=symbol,
                    side="buy" if i % 2 == 0 else "sell",
                    order_group_id=uuid.uuid4(),
                    requested_quantity=10,
                    filled_quantity=10 if i != 1 else 4,
                    avg_fill_price=100.0 + i,
                    fully_filled=i != 1,
                    data_source="real" if i == 2 else "synthetic",
                    costs_total=12.5 if i == 0 else 0.0,
                    administrative=i == 3,
                    created_at=base + timedelta(minutes=i),
                )
            )
        db.add(
            PaperPosition(
                subscription_id=unwatched.id, symbol=f"{symbol}X", quantity=7, avg_cost=250.0
            )
        )
        db.add(
            LivePosition(
                strategy_id=strategy.id,
                symbol=live_symbol,
                quantity=5,
                avg_cost=1500.0,
                realized_pnl=-300.5,
            )
        )

        accepted_intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol=live_symbol,
            side="buy",
            quantity=5,
            intent_type="entry",
            expires_at=base + timedelta(hours=1),
            generated_at=base + timedelta(hours=1),
            status="submitted",
        )
        refused_intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol=live_symbol,
            side="buy",
            quantity=50,
            intent_type="entry",
            expires_at=base + timedelta(hours=2),
            generated_at=base + timedelta(hours=2),
            status="failed",
        )
        capped_intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol=live_symbol,
            side="sell",
            quantity=5,
            intent_type="exit",
            expires_at=base + timedelta(hours=3),
            generated_at=base + timedelta(hours=3),
            status="capped",
        )
        db.add_all([accepted_intent, refused_intent, capped_intent])
        await db.flush()

        accepted = Order(
            live_order_intent_id=accepted_intent.id,
            strategy_id=strategy.id,
            symbol=live_symbol,
            side="buy",
            quantity=5,
            broker_name="zerodha",
            broker_order_id=f"E2E-OID-{suffix}",
            status="submitted",
            submitted_at=base + timedelta(hours=1),
        )
        refused = Order(
            live_order_intent_id=refused_intent.id,
            strategy_id=strategy.id,
            symbol=live_symbol,
            side="buy",
            quantity=50,
            broker_name="zerodha",
            status="failed",
            failure_reason=reason,
            submitted_at=base + timedelta(hours=2),
        )
        db.add_all([accepted, refused])
        await db.flush()
        accepted_trade = Trade(
            order_id=accepted.id,
            symbol=live_symbol,
            side="buy",
            quantity=5,
            price=1490.0,
            fill_price=1500.0,
            status="filled",
            data_source="real",
            executed_at=base + timedelta(hours=1, minutes=1),
        )
        refused_trade = Trade(
            order_id=refused.id,
            symbol=live_symbol,
            side="buy",
            quantity=50,
            price=1490.0,
            status="failed",
            data_source="real",
            executed_at=base + timedelta(hours=2, minutes=1),
        )
        db.add_all([accepted_trade, refused_trade])
        await db.commit()
        strategy_id = strategy.id
        trade_id = accepted_trade.id

    print(f"strategy_id={strategy_id}")
    print(f"name={name}")
    print(f"symbol={symbol}")
    print(f"live_symbol={live_symbol}")
    print(f"broker_order_id=E2E-OID-{suffix}")
    print(f"failure_reason={reason}")
    print(f"live_trade_id={trade_id}")
    print(f"paper_fills={PAPER_FILLS}")


if __name__ == "__main__":
    asyncio.run(seed())
