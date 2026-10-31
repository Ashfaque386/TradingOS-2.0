"""Investor Reporting Agent tests (Phase 19, docs/phase19-audit.md Part
2.5): real P&L/win-rate figures only, sourced from the same fields
`/pnl/today` and the Post-Trade Review Agent already use.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

from src.engine.paper_trading.market_hours import IST
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.orchestration.investor_reporting import generate_investor_report
from src.orchestration.strategies import create_strategy, create_version_with_validation

_VALID_CODE = "def run_backtest(data, config):\n    return {}\n"


async def test_generate_investor_report_reflects_real_paper_pnl(db_session_factory):
    from src.models.paper_fill import PaperFill

    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="InvestorReportDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="WIPRO", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="WIPRO",
                side="sell",
                order_group_id=uuid.uuid4(),
                requested_quantity=10,
                filled_quantity=10,
                avg_fill_price=500.0,
                fully_filled=True,
                realized_pnl=250.0,
            )
        )
        await db.commit()

    today = datetime.now(IST).date()
    async with db_session_factory() as db:
        report = await generate_investor_report(
            db, period_start=today - timedelta(days=1), period_end=today, cadence="weekly"
        )

    assert report.id is not None
    assert "250.00" in report.content_markdown
    assert "InvestorReportDemo" in report.content_markdown
    assert report.cadence == "weekly"


async def test_generate_investor_report_is_honest_about_a_quiet_period(db_session_factory):
    today = datetime.now(IST).date()
    async with db_session_factory() as db:
        report = await generate_investor_report(
            db, period_start=today - timedelta(days=7), period_end=today, cadence="weekly"
        )

    assert "No paper fills this period." in report.content_markdown
    assert "0.00" in report.content_markdown


async def test_a_period_is_the_ist_calendar_days_it_names(db_session_factory):
    """A fill at 22:00 UTC is 03:30 IST the next day: a report for that IST day includes it and
    the report for the day before does not."""
    from src.models.paper_fill import PaperFill

    async with db_session_factory() as db:
        strategy = await create_strategy(db, name="IstPeriodDemo", objective="obj")
        version = await create_version_with_validation(db, strategy, _VALID_CODE)
        subscription = PaperTradingSubscription(
            strategy_version_id=version.id, symbol="WIPRO", builtin_strategy="always_long"
        )
        db.add(subscription)
        await db.flush()
        db.add(
            PaperFill(
                subscription_id=subscription.id,
                symbol="WIPRO",
                side="sell",
                order_group_id=uuid.uuid4(),
                requested_quantity=10,
                filled_quantity=10,
                avg_fill_price=500.0,
                fully_filled=True,
                realized_pnl=321.0,
                created_at=datetime(2026, 3, 10, 22, 0, tzinfo=UTC),
            )
        )
        await db.commit()

    async with db_session_factory() as db:
        ist_day = await generate_investor_report(
            db, period_start=date(2026, 3, 11), period_end=date(2026, 3, 11), cadence="weekly"
        )
    async with db_session_factory() as db:
        utc_day = await generate_investor_report(
            db, period_start=date(2026, 3, 10), period_end=date(2026, 3, 10), cadence="weekly"
        )

    assert "321.00" in ist_day.content_markdown
    assert "321.00" not in utc_day.content_markdown
