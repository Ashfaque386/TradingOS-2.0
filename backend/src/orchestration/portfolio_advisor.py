"""Portfolio allocation recommendations (Phase 24, Build Spec extension,
docs/phase20-old-vs-new-comparison.md item 22): a real, advisory-only
rebalancing recommendation composed from each active strategy's real
latest completed backtest metrics and, when a broker is configured, real
margin -- explicitly never auto-executed (see
`PortfolioRecommendation`'s own docstring for the reasoning). Each
strategy's suggested action (`increase`/`decrease`/`hold`) is a
deterministic rule over its real `sharpe`/`max_drawdown` (see
`_allocation_action`) rather than something the LLM invents -- the LLM's
role is narrower: producing `summary`, a human-readable narrative over
those same real, already-decided numbers, the same
"real-work/LLM-narration" split `src.orchestration.post_trade_review`
already uses.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agents.llm_router import LlmRouter, LlmRouterExhaustedError, get_llm_router
from src.brokers.base import BrokerAdapter
from src.models.backtest_run import BacktestRun, BacktestStatus
from src.models.portfolio_recommendation import (
    PortfolioRecommendation,
    PortfolioRecommendationStatus,
)
from src.models.strategy import Strategy, StrategyStatus
from src.models.strategy_version import StrategyVersion

# Strategies genuinely consuming (or about to consume) capital -- a
# recommendation to "increase"/"decrease" allocation is meaningless for
# something still in Ideation/Coding/Backtesting, and Deprecated is
# retired.
ALLOCATABLE_STRATEGY_STATUSES = (
    StrategyStatus.PAPER_TRADING.value,
    StrategyStatus.LIVE_ELIGIBLE.value,
    StrategyStatus.LIVE.value,
)

# Thresholds are deliberately simple and stated here, not hidden in a
# config -- an operator reading a real financial recommendation should be
# able to see exactly what real numbers produced it.
_INCREASE_SHARPE_MIN = 1.0
_INCREASE_MAX_DRAWDOWN_CEILING = 0.15
_DECREASE_SHARPE_MAX = 0.0
_DECREASE_MAX_DRAWDOWN_FLOOR = 0.25


async def _latest_completed_backtest_metrics(
    db: AsyncSession, strategy_id: uuid.UUID
) -> dict | None:
    result = await db.execute(
        select(BacktestRun.metrics)
        .join(StrategyVersion, StrategyVersion.id == BacktestRun.strategy_version_id)
        .where(
            StrategyVersion.strategy_id == strategy_id,
            BacktestRun.status == BacktestStatus.COMPLETED,
        )
        .order_by(BacktestRun.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


def _allocation_action(metrics: dict | None) -> tuple[str, str]:
    if not metrics:
        return "hold", "no completed backtest yet -- insufficient data to recommend a change"

    sharpe = metrics.get("sharpe")
    max_drawdown = metrics.get("max_drawdown")

    if (
        sharpe is not None
        and sharpe > _INCREASE_SHARPE_MIN
        and (max_drawdown is None or max_drawdown < _INCREASE_MAX_DRAWDOWN_CEILING)
    ):
        drawdown_note = "n/a" if max_drawdown is None else f"{max_drawdown:.1%} drawdown"
        return "increase", f"sharpe {sharpe:.2f} with {drawdown_note} supports more allocation"

    reasons = []
    if sharpe is not None and sharpe < _DECREASE_SHARPE_MAX:
        reasons.append(f"negative sharpe ({sharpe:.2f})")
    if max_drawdown is not None and max_drawdown > _DECREASE_MAX_DRAWDOWN_FLOOR:
        reasons.append(f"{max_drawdown:.1%} drawdown")
    if reasons:
        return "decrease", f"{' and '.join(reasons)} suggest reducing allocation"

    return "hold", "metrics within normal range -- no allocation change recommended"


async def _margin_snapshot(adapter: BrokerAdapter | None) -> dict | None:
    if adapter is None:
        return None
    try:
        margin = await adapter.get_margin()
    except Exception:  # noqa: BLE001 - a broker/network hiccup must never block a recommendation
        return None
    return {"available_margin": margin.available_margin, "used_margin": margin.used_margin}


def _fallback_summary(allocations: list[dict], margin: dict | None) -> str:
    increases = [a["strategy_name"] for a in allocations if a["action"] == "increase"]
    decreases = [a["strategy_name"] for a in allocations if a["action"] == "decrease"]
    parts = [
        "fallback summary (no LLM provider reachable): the allocation "
        "decisions below are from real backtest metrics only, unnarrated."
    ]
    if increases:
        parts.append(f"Consider increasing: {', '.join(increases)}.")
    if decreases:
        parts.append(f"Consider decreasing: {', '.join(decreases)}.")
    if not increases and not decreases:
        parts.append("No strategy currently meets the increase/decrease thresholds.")
    if margin is None:
        parts.append("No broker is currently configured, so margin was not available.")
    return " ".join(parts)


def _build_prompt(allocations: list[dict], margin: dict | None) -> str:
    lines = [
        "You are a portfolio manager summarizing a rebalancing review for a "
        "human operator. This is advisory only -- nothing you say will be "
        "auto-executed as a trade. Each strategy's action below was already "
        "decided from real backtest metrics, not by you; write a short, "
        "plain-English narrative explaining the overall picture.",
        "",
        "Active strategies and their decided action:",
    ]
    for a in allocations:
        lines.append(
            f"- {a['strategy_name']} ({a['strategy_status']}): {a['action']} -- {a['rationale']}"
        )
    if margin is not None:
        lines.append(
            f"\nAvailable margin: {margin['available_margin']:.2f}, "
            f"used margin: {margin['used_margin']:.2f}."
        )
    else:
        lines.append("\nNo broker is currently configured -- margin is unavailable.")
    return "\n".join(lines)


async def generate_recommendation(
    db: AsyncSession,
    *,
    router: LlmRouter | None = None,
    adapter: BrokerAdapter | None = None,
) -> PortfolioRecommendation:
    result = await db.execute(
        select(Strategy)
        .where(Strategy.status.in_(ALLOCATABLE_STRATEGY_STATUSES))
        .order_by(Strategy.name)
    )
    strategies = result.scalars().all()

    allocations: list[dict] = []
    for strategy in strategies:
        metrics = await _latest_completed_backtest_metrics(db, strategy.id)
        action, rationale = _allocation_action(metrics)
        allocations.append(
            {
                "strategy_id": str(strategy.id),
                "strategy_name": strategy.name,
                "strategy_status": strategy.status,
                "action": action,
                "rationale": rationale,
                "sharpe": metrics.get("sharpe") if metrics else None,
                "max_drawdown": metrics.get("max_drawdown") if metrics else None,
            }
        )

    margin = await _margin_snapshot(adapter)
    based_on = {
        "strategy_count": len(strategies),
        "margin": margin,
        "generated_at": datetime.now(UTC).isoformat(),
    }

    router = router or get_llm_router()
    try:
        completion = await router.complete(
            agent_id="portfolio-manager-agent", prompt=_build_prompt(allocations, margin)
        )
        summary = completion.text
        llm_source = "llm"
    except LlmRouterExhaustedError:
        summary = _fallback_summary(allocations, margin)
        llm_source = "fallback"

    recommendation = PortfolioRecommendation(
        summary=summary,
        allocations=allocations,
        based_on=based_on,
        llm_source=llm_source,
        status=PortfolioRecommendationStatus.PENDING,
    )
    db.add(recommendation)
    await db.commit()
    await db.refresh(recommendation)
    return recommendation


async def decide_recommendation(
    db: AsyncSession,
    recommendation_id: uuid.UUID,
    *,
    accept: bool,
    reviewed_by: str,
    notes: str | None = None,
) -> PortfolioRecommendation | None:
    """Mirrors `src.orchestration.approvals.decide_approval_request`'s own
    shape exactly: `None` means not found or already decided, so the route
    can 404 either way without leaking which. Never touches a strategy,
    order, or broker -- see `PortfolioRecommendation`'s docstring."""
    recommendation = await db.get(PortfolioRecommendation, recommendation_id)
    if recommendation is None or recommendation.status != PortfolioRecommendationStatus.PENDING:
        return None

    recommendation.status = (
        PortfolioRecommendationStatus.ACCEPTED if accept else PortfolioRecommendationStatus.REJECTED
    )
    recommendation.reviewed_by = reviewed_by
    recommendation.reviewed_at = datetime.now(UTC)
    recommendation.reviewer_notes = notes
    await db.commit()
    await db.refresh(recommendation)
    return recommendation
