"""Post-Trade Review Agent (Phase 19, docs/phase19-audit.md Part 2.4): runs
after market close, reviews the day's closed paper/live trades against the
strategies that generated them, and writes `TradeReviewFinding` rows the
next real "revisit this strategy" cycle can read.

Real, not fabricated: paper-trade figures come straight from
`PaperFill.realized_pnl`, the same field `/api/v1/paper-trading/pnl/today`
already aggregates globally (Phase 16 Follow-up F) -- this module only adds
a per-strategy `GROUP BY`, it does not re-derive P&L. Live trades have no
per-trade realized-P&L field anywhere in this codebase (`Trade` only
carries `price`/`fill_price`), so a live-mode finding reports a real fill
count plus `LivePosition.realized_pnl` (a running cumulative total)
labeled `pnl_scope="cumulative_to_date"` -- never blended with the
same-day paper figure, matching this codebase's established pattern of
never conflating two different scopes under one number (Phase 16 Follow-up
F's realized-vs-unrealized P&L split is the precedent).
"""

from datetime import UTC, date, datetime, time, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.live_position import LivePosition
from src.models.order import Order
from src.models.paper_fill import PaperFill
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.strategy_version import StrategyVersion
from src.models.trade import Trade
from src.models.trade_review_finding import TradeReviewFinding

logger = structlog.get_logger(__name__)

POST_TRADE_REVIEW_ACTOR = "system:post-trade-review-agent"


def _day_bounds_utc(as_of: date) -> tuple[datetime, datetime]:
    start = datetime.combine(as_of, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


async def _review_paper_trades(db: AsyncSession, *, as_of: date) -> list[TradeReviewFinding]:
    start, end = _day_bounds_utc(as_of)
    rows = (
        await db.execute(
            select(
                StrategyVersion.strategy_id,
                func.count(PaperFill.id),
                func.sum(func.coalesce(PaperFill.realized_pnl, 0.0)),
            )
            .select_from(PaperFill)
            .join(
                PaperTradingSubscription, PaperFill.subscription_id == PaperTradingSubscription.id
            )
            .join(
                StrategyVersion, PaperTradingSubscription.strategy_version_id == StrategyVersion.id
            )
            .where(PaperFill.created_at >= start, PaperFill.created_at < end)
            .group_by(StrategyVersion.strategy_id)
        )
    ).all()

    findings: list[TradeReviewFinding] = []
    for strategy_id, trades_reviewed, total_pnl in rows:
        # Only fills that actually closed a position carry a nonzero
        # realized_pnl (Phase 7's average-cost-basis accounting leaves
        # entry fills at exactly 0.0) -- win/loss counts are computed over
        # those closing fills only, never over entries.
        closing = (
            (
                await db.execute(
                    select(PaperFill.realized_pnl)
                    .join(
                        PaperTradingSubscription,
                        PaperFill.subscription_id == PaperTradingSubscription.id,
                    )
                    .join(
                        StrategyVersion,
                        PaperTradingSubscription.strategy_version_id == StrategyVersion.id,
                    )
                    .where(
                        StrategyVersion.strategy_id == strategy_id,
                        PaperFill.created_at >= start,
                        PaperFill.created_at < end,
                        PaperFill.realized_pnl != 0.0,
                    )
                )
            )
            .scalars()
            .all()
        )

        winning = sum(1 for pnl in closing if pnl > 0)
        losing = sum(1 for pnl in closing if pnl < 0)
        win_rate = winning / len(closing) if closing else None

        if closing:
            commentary = (
                f"{trades_reviewed} paper fill(s) today, {len(closing)} closing "
                f"({winning} winning, {losing} losing, win rate "
                f"{win_rate:.0%}). Realized P&L today: {float(total_pnl or 0.0):.2f}."
            )
        else:
            commentary = (
                f"{trades_reviewed} paper fill(s) today, none closed a position yet "
                f"(all entries) -- no win/loss figure to report."
            )

        findings.append(
            TradeReviewFinding(
                strategy_id=strategy_id,
                review_date=as_of,
                mode="paper",
                trades_reviewed=int(trades_reviewed),
                winning_trades=winning if closing else None,
                losing_trades=losing if closing else None,
                win_rate=win_rate,
                realized_pnl=float(total_pnl or 0.0),
                pnl_scope="today",
                commentary=commentary,
            )
        )
    return findings


async def _review_live_trades(db: AsyncSession, *, as_of: date) -> list[TradeReviewFinding]:
    start, end = _day_bounds_utc(as_of)
    rows = (
        await db.execute(
            select(Order.strategy_id, func.count(Trade.id))
            .select_from(Trade)
            .join(Order, Trade.order_id == Order.id)
            .where(Trade.status == "filled", Trade.executed_at >= start, Trade.executed_at < end)
            .group_by(Order.strategy_id)
        )
    ).all()

    findings: list[TradeReviewFinding] = []
    for strategy_id, trades_reviewed in rows:
        positions = (
            (await db.execute(select(LivePosition).where(LivePosition.strategy_id == strategy_id)))
            .scalars()
            .all()
        )
        cumulative_pnl = sum(p.realized_pnl for p in positions) if positions else None

        commentary = (
            f"{trades_reviewed} live trade(s) filled today. No per-trade realized "
            f"P&L is tracked for live trades in this codebase (see this module's "
            f"docstring), so no win/loss split is reported here -- cumulative "
            f"realized P&L to date across this strategy's live position(s): "
            f"{cumulative_pnl:.2f}."
            if cumulative_pnl is not None
            else f"{trades_reviewed} live trade(s) filled today. No open/closed "
            f"live position recorded for this strategy yet."
        )

        findings.append(
            TradeReviewFinding(
                strategy_id=strategy_id,
                review_date=as_of,
                mode="live",
                trades_reviewed=int(trades_reviewed),
                winning_trades=None,
                losing_trades=None,
                win_rate=None,
                realized_pnl=cumulative_pnl,
                pnl_scope="cumulative_to_date",
                commentary=commentary,
            )
        )
    return findings


async def run_post_trade_review(db: AsyncSession, *, as_of: date) -> list[TradeReviewFinding]:
    """Reviews every strategy with a closed paper or live trade on `as_of`,
    writes one `TradeReviewFinding` row per strategy per mode. Idempotent
    per (strategy, mode, review_date): re-running for a date that already
    has findings replaces them rather than duplicating, so a scheduler
    retry after a partial failure can't double-count.
    """
    existing = (
        (
            await db.execute(
                select(TradeReviewFinding).where(TradeReviewFinding.review_date == as_of)
            )
        )
        .scalars()
        .all()
    )
    for row in existing:
        await db.delete(row)

    findings = await _review_paper_trades(db, as_of=as_of) + await _review_live_trades(
        db, as_of=as_of
    )
    for finding in findings:
        db.add(finding)
    await db.commit()

    logger.info(
        "post_trade_review.completed", review_date=as_of.isoformat(), findings=len(findings)
    )
    return findings


async def latest_finding_for_strategy(db: AsyncSession, strategy_id) -> TradeReviewFinding | None:
    """Reads the most recent finding for a strategy -- the real
    consumption point `src.orchestration.strategy_suggestions.review_suggestion`
    calls before building its LLM prompt, so the Strategy Generator/
    Evaluator agents' "next cycle" for an existing strategy actually sees
    this, not a report nobody reads."""
    result = await db.execute(
        select(TradeReviewFinding)
        .where(TradeReviewFinding.strategy_id == strategy_id)
        .order_by(TradeReviewFinding.review_date.desc(), TradeReviewFinding.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()
