"""Portfolio API (Phase 24, docs/phase20-old-vs-new-comparison.md items 22
and 23): advisory-only rebalancing recommendations with a human
accept/reject audit trail (item 22, `src.orchestration.portfolio_advisor`),
plus a single cross-broker-ready dashboard rollup (item 23) composing
real paper P&L, real live positions, real active-strategy count, and
real broker margin (when a broker is configured) into one view -- none of
which existed as a single endpoint before this phase.
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    DecidePortfolioRecommendationRequest,
    PortfolioExposureEntry,
    PortfolioRecommendationResponse,
    PortfolioRiskMetricsResponse,
    PortfolioSummaryResponse,
)
from src.brokers.base import BrokerAdapter
from src.brokers.factory import build_configured_adapter
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.live_position import LivePosition
from src.models.paper_fill import PaperFill
from src.models.paper_position import PaperPosition
from src.models.paper_trading_subscription import PaperTradingSubscription
from src.models.portfolio_recommendation import (
    PortfolioRecommendation,
    PortfolioRecommendationStatus,
)
from src.models.strategy import Strategy
from src.models.strategy_version import StrategyVersion
from src.models.user import User
from src.orchestration.portfolio_advisor import (
    ALLOCATABLE_STRATEGY_STATUSES,
    decide_recommendation,
    generate_recommendation,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])

# Deciding a recommendation is a portfolio-governance action, same
# SystemAdministrator-retained-for-break-glass posture as approvals.py's
# own _DECIDE_ROLES.
_MANAGE_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("GET", "/api/v1/portfolio/summary", roles=list(Role))
register_policy("GET", "/api/v1/portfolio/risk-metrics", roles=list(Role))
register_policy("GET", "/api/v1/portfolio/recommendations", roles=list(Role))
register_policy("GET", "/api/v1/portfolio/recommendations/{recommendation_id}", roles=list(Role))
register_policy("POST", "/api/v1/portfolio/recommendations/generate", roles=_MANAGE_ROLES)
register_policy(
    "POST", "/api/v1/portfolio/recommendations/{recommendation_id}/accept", roles=_MANAGE_ROLES
)
register_policy(
    "POST", "/api/v1/portfolio/recommendations/{recommendation_id}/reject", roles=_MANAGE_ROLES
)


def get_portfolio_broker_adapter() -> BrokerAdapter | None:
    """Same `Depends`-wrapped seam as
    `src.api.routes.market_data.get_market_data_broker_adapter` -- a
    read-only margin query is safe against the production-pointed
    adapter, and tests override this dependency directly."""
    return build_configured_adapter(sandbox=False)


@router.post("/recommendations/generate")
async def generate_recommendation_endpoint(
    db: AsyncSession = Depends(get_db),
    adapter: BrokerAdapter | None = Depends(get_portfolio_broker_adapter),
    _current_user: User = Depends(require_role),
) -> PortfolioRecommendationResponse:
    recommendation = await generate_recommendation(db, adapter=adapter)
    return PortfolioRecommendationResponse.model_validate(recommendation)


@router.get("/recommendations")
async def list_recommendations_endpoint(
    status_filter: PortfolioRecommendationStatus | None = None,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[PortfolioRecommendationResponse]:
    query = select(PortfolioRecommendation).order_by(PortfolioRecommendation.created_at.desc())
    if status_filter is not None:
        query = query.where(PortfolioRecommendation.status == status_filter)
    result = await db.execute(query)
    return [PortfolioRecommendationResponse.model_validate(row) for row in result.scalars().all()]


@router.get("/recommendations/{recommendation_id}")
async def get_recommendation_endpoint(
    recommendation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> PortfolioRecommendationResponse:
    recommendation = await db.get(PortfolioRecommendation, recommendation_id)
    if recommendation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such recommendation")
    return PortfolioRecommendationResponse.model_validate(recommendation)


@router.post("/recommendations/{recommendation_id}/accept")
async def accept_recommendation_endpoint(
    recommendation_id: uuid.UUID,
    body: DecidePortfolioRecommendationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> PortfolioRecommendationResponse:
    decided = await decide_recommendation(
        db, recommendation_id, accept=True, reviewed_by=current_user.email, notes=body.notes
    )
    if decided is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recommendation not found or already decided",
        )
    return PortfolioRecommendationResponse.model_validate(decided)


@router.post("/recommendations/{recommendation_id}/reject")
async def reject_recommendation_endpoint(
    recommendation_id: uuid.UUID,
    body: DecidePortfolioRecommendationRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> PortfolioRecommendationResponse:
    decided = await decide_recommendation(
        db, recommendation_id, accept=False, reviewed_by=current_user.email, notes=body.notes
    )
    if decided is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recommendation not found or already decided",
        )
    return PortfolioRecommendationResponse.model_validate(decided)


@router.get("/summary")
async def portfolio_summary_endpoint(
    db: AsyncSession = Depends(get_db),
    adapter: BrokerAdapter | None = Depends(get_portfolio_broker_adapter),
    _current_user: User = Depends(require_role),
) -> PortfolioSummaryResponse:
    """A single cross-broker-ready dashboard rollup (item 23) -- real
    active-strategy count, real paper P&L/open-position count, real live
    position count, and real broker margin when one is configured, never
    blended or fabricated when a source is unavailable (`available_margin`/
    `used_margin` stay `null`, not `0.0`, exactly like every other
    optional-when-unconfigured numeric field in this codebase)."""
    active_strategy_count = (
        await db.execute(
            select(func.count(Strategy.id)).where(
                Strategy.status.in_(ALLOCATABLE_STRATEGY_STATUSES)
            )
        )
    ).scalar_one()

    start_of_day_utc = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    paper_realized_pnl_today = (
        await db.execute(
            select(func.coalesce(func.sum(PaperFill.realized_pnl), 0.0)).where(
                PaperFill.created_at >= start_of_day_utc
            )
        )
    ).scalar_one()

    paper_open_position_count = (
        await db.execute(select(func.count(PaperPosition.id)).where(PaperPosition.quantity != 0))
    ).scalar_one()

    live_position_count = (
        await db.execute(select(func.count(LivePosition.id)).where(LivePosition.quantity != 0))
    ).scalar_one()

    available_margin: float | None = None
    used_margin: float | None = None
    if adapter is not None:
        try:
            margin = await adapter.get_margin()
            available_margin = margin.available_margin
            used_margin = margin.used_margin
        except Exception:  # noqa: BLE001 - a broker hiccup must never break the dashboard
            pass

    return PortfolioSummaryResponse(
        as_of=datetime.now(UTC).isoformat(),
        active_strategy_count=active_strategy_count,
        paper_realized_pnl_today=float(paper_realized_pnl_today),
        paper_open_position_count=paper_open_position_count,
        live_position_count=live_position_count,
        broker_configured=adapter is not None,
        broker_name=adapter.broker_name if adapter is not None else None,
        available_margin=available_margin,
        used_margin=used_margin,
    )


@router.get("/risk-metrics")
async def portfolio_risk_metrics_endpoint(
    db: AsyncSession = Depends(get_db),
    adapter: BrokerAdapter | None = Depends(get_portfolio_broker_adapter),
    _current_user: User = Depends(require_role),
) -> PortfolioRiskMetricsResponse:
    """The one item-23 piece `/portfolio/summary` deliberately left out
    (docs/phase20-old-vs-new-comparison.md item 23's `/portfolio/risk-metrics`).
    Real exposure per open position (`abs(quantity * avg_cost)`, the same
    average-cost-basis convention `PaperPosition`/`LivePosition` already
    use) and a real concentration ratio -- never a fabricated VaR model or
    a comparison against a configured limit that doesn't exist in this
    codebase (the only two named `RiskLimit`s today are `max_drawdown_pct`
    and `ws_latency_ms`, neither a concentration/leverage threshold) --
    the operator reads the real number and judges it themselves. Margin
    utilization is `null`, not `0.0`, when no broker is configured or it
    has zero total margin to divide by."""
    live_rows = (
        await db.execute(
            select(LivePosition, Strategy.name)
            .join(Strategy, LivePosition.strategy_id == Strategy.id)
            .where(LivePosition.quantity != 0)
        )
    ).all()
    paper_rows = (
        await db.execute(
            select(PaperPosition, Strategy.id, Strategy.name)
            .join(
                PaperTradingSubscription,
                PaperPosition.subscription_id == PaperTradingSubscription.id,
            )
            .join(
                StrategyVersion, PaperTradingSubscription.strategy_version_id == StrategyVersion.id
            )
            .join(Strategy, StrategyVersion.strategy_id == Strategy.id)
            .where(PaperPosition.quantity != 0)
        )
    ).all()

    exposure_by_position = [
        PortfolioExposureEntry(
            mode="live",
            strategy_id=str(position.strategy_id),
            strategy_name=strategy_name,
            symbol=position.symbol,
            exposure=abs(position.quantity * position.avg_cost),
        )
        for position, strategy_name in live_rows
    ] + [
        PortfolioExposureEntry(
            mode="paper",
            strategy_id=str(strategy_id),
            strategy_name=strategy_name,
            symbol=position.symbol,
            exposure=abs(position.quantity * position.avg_cost),
        )
        for position, strategy_id, strategy_name in paper_rows
    ]
    exposure_by_position.sort(key=lambda e: e.exposure, reverse=True)

    total_exposure = sum(e.exposure for e in exposure_by_position)
    largest_position_concentration_pct = (
        (exposure_by_position[0].exposure / total_exposure * 100.0)
        if exposure_by_position and total_exposure > 0
        else None
    )

    margin_utilization_pct: float | None = None
    if adapter is not None:
        try:
            margin = await adapter.get_margin()
            total_margin = margin.available_margin + margin.used_margin
            if total_margin > 0:
                margin_utilization_pct = margin.used_margin / total_margin * 100.0
        except Exception:  # noqa: BLE001 - a broker hiccup must never break the dashboard
            pass

    return PortfolioRiskMetricsResponse(
        as_of=datetime.now(UTC).isoformat(),
        open_position_count=len(exposure_by_position),
        total_exposure=total_exposure,
        exposure_by_position=exposure_by_position,
        largest_position_concentration_pct=largest_position_concentration_pct,
        broker_configured=adapter is not None,
        margin_utilization_pct=margin_utilization_pct,
    )
