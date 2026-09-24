"""Post-Trade Review Agent tests (Phase 19, docs/phase19-audit.md Part
2.4): real figures only -- paper trades from `PaperFill.realized_pnl`
(the same field `/pnl/today` aggregates), live trades from a real fill
count + `LivePosition.realized_pnl` (explicitly labeled
`cumulative_to_date`, never blended with the same-day paper figure).
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from src.models.live_order_intent import LiveOrderIntent
from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.trade import Trade
from src.models.trade_review_finding import TradeReviewFinding
from src.orchestration.post_trade_review import latest_finding_for_strategy, run_post_trade_review
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def test_review_reports_real_win_loss_and_pnl_for_paper_trades(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="PaperReviewDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="RELIANCE", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()

        # An entry fill (realized_pnl stays 0.0 -- Phase 7's own convention)
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="RELIANCE",
                side="buy",
                order_group_id=uuid.uuid4(),
                requested_quantity=10,
                filled_quantity=10,
                avg_fill_price=2500.0,
                fully_filled=True,
                realized_pnl=0.0,
            )
        )
        # A winning exit
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="RELIANCE",
                side="sell",
                order_group_id=uuid.uuid4(),
                requested_quantity=10,
                filled_quantity=10,
                avg_fill_price=2600.0,
                fully_filled=True,
                realized_pnl=1000.0,
            )
        )
        # A losing exit
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="RELIANCE",
                side="sell",
                order_group_id=uuid.uuid4(),
                requested_quantity=5,
                filled_quantity=5,
                avg_fill_price=2400.0,
                fully_filled=True,
                realized_pnl=-200.0,
            )
        )
        await db.commit()
        strategy_id = strategy.id

    async with db_session_factory() as db:
        findings = await run_post_trade_review(db, as_of=date.today())

    paper = next(f for f in findings if f.strategy_id == strategy_id and f.mode == "paper")
    assert paper.trades_reviewed == 3
    assert paper.winning_trades == 1
    assert paper.losing_trades == 1
    assert paper.win_rate == 0.5
    assert paper.realized_pnl == 800.0
    assert paper.pnl_scope == "today"
    assert "800" in paper.commentary or "800.00" in paper.commentary


async def test_review_reports_live_fills_with_cumulative_pnl_never_blended_with_today(
    db_session_factory,
):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="LiveReviewDemo", objective="obj")
        db.add(
            LivePosition(
                strategy_id=strategy.id,
                symbol="TCS",
                quantity=10,
                avg_cost=3400.0,
                realized_pnl=500.0,
            )
        )

        intent = LiveOrderIntent(
            strategy_id=strategy.id,
            symbol="TCS",
            side="buy",
            quantity=10,
            intent_type="entry",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            status="approved",
        )
        db.add(intent)
        await db.flush()

        order = Order(
            live_order_intent_id=intent.id,
            strategy_id=strategy.id,
            symbol="TCS",
            side="buy",
            quantity=10,
            broker_name="zerodha",
            status="submitted",
        )
        db.add(order)
        await db.flush()

        db.add(
            Trade(
                order_id=order.id,
                symbol="TCS",
                side="buy",
                quantity=10,
                price=3400.0,
                status="filled",
            )
        )
        await db.commit()
        strategy_id = strategy.id

    async with db_session_factory() as db:
        findings = await run_post_trade_review(db, as_of=date.today())

    live = next(f for f in findings if f.strategy_id == strategy_id and f.mode == "live")
    assert live.trades_reviewed == 1
    assert live.winning_trades is None  # no per-trade realized P&L exists for live, honestly None
    assert live.losing_trades is None
    assert live.pnl_scope == "cumulative_to_date"
    assert live.realized_pnl == 500.0


async def test_run_post_trade_review_is_idempotent_per_date(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="IdempotentReview", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="INFY", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="INFY",
                side="buy",
                order_group_id=uuid.uuid4(),
                requested_quantity=1,
                filled_quantity=1,
                avg_fill_price=100.0,
                fully_filled=True,
                realized_pnl=0.0,
            )
        )
        await db.commit()

    async with db_session_factory() as db:
        await run_post_trade_review(db, as_of=date.today())
        await run_post_trade_review(db, as_of=date.today())

        rows = (
            (
                await db.execute(
                    select(TradeReviewFinding).where(TradeReviewFinding.review_date == date.today())
                )
            )
            .scalars()
            .all()
        )

    # Exactly one row per (strategy, mode) for the date, not duplicated
    matching = [r for r in rows if r.mode == "paper"]
    assert len(matching) == len({r.strategy_id for r in matching})


async def test_latest_finding_for_strategy_returns_none_when_never_reviewed(db_session_factory):
    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="NeverReviewed", objective="obj")
        await db.commit()
        result = await latest_finding_for_strategy(db, strategy.id)

    assert result is None
