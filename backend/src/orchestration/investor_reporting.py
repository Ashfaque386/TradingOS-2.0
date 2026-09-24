"""Investor Reporting Agent (Phase 19, docs/phase19-audit.md Part 2.5): a
real markdown performance narrative on a configurable cadence (weekly by
default), sourced from the same real figures every other report in this
app already uses -- `PaperFill.realized_pnl` (the same field `/pnl/today`
aggregates, Phase 16 Follow-up F) and `LivePosition.realized_pnl` -- never
a fabricated number. A period with zero trades still produces a real,
honest report saying so, not a suppressed one (same posture as the
notification and audit schedulers).
"""

from datetime import UTC, date, datetime, time, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.investor_report import InvestorReport
from src.models.live_position import LivePosition
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy import Strategy
from src.models.strategy_version import StrategyVersion
from src.models.trade import Trade
from src.models.trade_review_finding import TradeReviewFinding

logger = structlog.get_logger(__name__)

INVESTOR_REPORTING_ACTOR = "system:investor-reporting-agent"


def _period_bounds_utc(period_start: date, period_end: date) -> tuple[datetime, datetime]:
    start = datetime.combine(period_start, time.min, tzinfo=UTC)
    end = datetime.combine(period_end, time.min, tzinfo=UTC) + timedelta(days=1)
    return start, end


async def generate_investor_report(
    db: AsyncSession, *, period_start: date, period_end: date, cadence: str = "weekly"
) -> InvestorReport:
    start, end = _period_bounds_utc(period_start, period_end)

    total_realized_pnl = (
        await db.execute(
            select(func.coalesce(func.sum(PaperFill.realized_pnl), 0.0)).where(
                PaperFill.created_at >= start, PaperFill.created_at < end
            )
        )
    ).scalar_one()

    closing_fills = (
        (
            await db.execute(
                select(PaperFill.realized_pnl).where(
                    PaperFill.created_at >= start,
                    PaperFill.created_at < end,
                    PaperFill.realized_pnl != 0.0,
                )
            )
        )
        .scalars()
        .all()
    )
    winning = sum(1 for pnl in closing_fills if pnl > 0)
    losing = sum(1 for pnl in closing_fills if pnl < 0)
    win_rate = winning / len(closing_fills) if closing_fills else None

    per_strategy = (
        await db.execute(
            select(
                Strategy.name,
                func.count(PaperFill.id),
                func.coalesce(func.sum(PaperFill.realized_pnl), 0.0),
            )
            .select_from(PaperFill)
            .join(
                PaperTradingSubscription, PaperFill.subscription_id == PaperTradingSubscription.id
            )
            .join(
                StrategyVersion, PaperTradingSubscription.strategy_version_id == StrategyVersion.id
            )
            .join(Strategy, StrategyVersion.strategy_id == Strategy.id)
            .where(PaperFill.created_at >= start, PaperFill.created_at < end)
            .group_by(Strategy.name)
            .order_by(func.count(PaperFill.id).desc())
        )
    ).all()

    live_trades_filled = (
        await db.execute(
            select(func.count(Trade.id)).where(
                Trade.status == "filled", Trade.executed_at >= start, Trade.executed_at < end
            )
        )
    ).scalar_one()
    live_cumulative_pnl = (
        await db.execute(select(func.coalesce(func.sum(LivePosition.realized_pnl), 0.0)))
    ).scalar_one()

    review_findings = (
        (
            await db.execute(
                select(TradeReviewFinding).where(
                    TradeReviewFinding.review_date >= period_start,
                    TradeReviewFinding.review_date <= period_end,
                )
            )
        )
        .scalars()
        .all()
    )

    lines = [
        f"# Investor Report: {period_start.isoformat()} to {period_end.isoformat()} ({cadence})",
        "",
        "## Paper Trading Performance",
        "",
        f"- Realized P&L this period: {float(total_realized_pnl):.2f}",
        f"- Closing trades: {len(closing_fills)} ({winning} winning, {losing} losing)"
        + (
            f", win rate {win_rate:.0%}"
            if win_rate is not None
            else ", no win rate available (no closing trades)"
        ),
        "",
        "## By Strategy",
        "",
    ]
    if per_strategy:
        for name, fills, pnl in per_strategy:
            lines.append(f"- **{name}**: {fills} fill(s), realized P&L {float(pnl):.2f}")
    else:
        lines.append("- No paper fills this period.")

    lines += [
        "",
        "## Live Trading",
        "",
        f"- Live trades filled this period: {live_trades_filled}",
        f"- Cumulative realized P&L across all live positions (to date, not scoped to "
        f"this period -- see this module's own docstring): {float(live_cumulative_pnl):.2f}",
        "",
        "## Post-Trade Review Commentary",
        "",
    ]
    if review_findings:
        for finding in review_findings:
            lines.append(
                f"- {finding.review_date.isoformat()} ({finding.mode}): {finding.commentary}"
            )
    else:
        lines.append("- No post-trade review findings recorded this period.")

    content = "\n".join(lines) + "\n"

    report = InvestorReport(
        period_start=period_start,
        period_end=period_end,
        cadence=cadence,
        content_markdown=content,
        generated_by=INVESTOR_REPORTING_ACTOR,
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)

    logger.info(
        "investor_reporting.report_generated",
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        realized_pnl=float(total_realized_pnl),
    )
    return report
