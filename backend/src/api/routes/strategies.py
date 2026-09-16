"""Strategy pipeline API routes (Build Spec §9). Phase 4 acceptance:
submitting an objective produces a generated strategy that passes static
validation, runs inside the sandbox, and cannot reach Backtesting ->
PaperTrading without a human approval (src/api/routes/approvals.py decides
the ApprovalRequest this flow's /promote depends on).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    ApprovalRequestResponse,
    CreateStrategyRequest,
    StrategyResponse,
    StrategyVersionResponse,
    SubmitSuggestionRequest,
    SuggestionResponse,
)
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.models.strategy import Strategy
from src.models.strategy_suggestion import StrategySuggestion
from src.models.strategy_version import StrategyVersion
from src.models.user import User
from src.orchestration import strategies as strategies_orch
from src.orchestration import strategy_suggestions as suggestions_orch
from src.orchestration.approvals import create_approval_request
from src.orchestration.strategies import (
    PROMOTION_TRANSITION_TYPE,
    CorrelationConstraintBreachedError,
)
from src.orchestration.transitions import ApprovalRequiredError

router = APIRouter(prefix="/strategies", tags=["strategies"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]

register_policy("POST", "/api/v1/strategies", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/strategies/{strategy_id}", roles=list(Role))
register_policy("POST", "/api/v1/strategies/{strategy_id}/suggestions", roles=list(Role))
register_policy(
    "POST",
    "/api/v1/strategies/{strategy_id}/suggestions/{suggestion_id}/review",
    roles=_OPERATOR_ROLES,
)
register_policy(
    "POST",
    "/api/v1/strategies/{strategy_id}/suggestions/{suggestion_id}/regenerate",
    roles=_OPERATOR_ROLES,
)
register_policy("POST", "/api/v1/strategies/{strategy_id}/request-promotion", roles=_OPERATOR_ROLES)
register_policy("POST", "/api/v1/strategies/{strategy_id}/promote", roles=_OPERATOR_ROLES)


async def _load_strategy_response(db: AsyncSession, strategy_id: uuid.UUID) -> StrategyResponse:
    strategy = await db.get(Strategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")

    versions_result = await db.execute(
        select(StrategyVersion)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_number)
    )
    versions = list(versions_result.scalars())
    return StrategyResponse(
        id=strategy.id,
        name=strategy.name,
        objective=strategy.objective,
        instrument_class=strategy.instrument_class,
        status=strategy.status,
        versions=[StrategyVersionResponse.model_validate(v) for v in versions],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_strategy_endpoint(
    body: CreateStrategyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> StrategyResponse:
    strategy, _version = await strategies_orch.run_strategy_pipeline(
        db,
        name=body.name,
        objective=body.objective,
        instrument_class=body.instrument_class,
        created_by=str(current_user.id),
    )
    return await _load_strategy_response(db, strategy.id)


@router.get("/{strategy_id}")
async def get_strategy_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> StrategyResponse:
    return await _load_strategy_response(db, strategy_id)


@router.post("/{strategy_id}/suggestions", status_code=status.HTTP_201_CREATED)
async def submit_suggestion_endpoint(
    strategy_id: uuid.UUID,
    body: SubmitSuggestionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> SuggestionResponse:
    suggestion = await suggestions_orch.submit_suggestion(
        db,
        strategy_id=strategy_id,
        base_version_id=body.base_version_id,
        suggestion_text=body.suggestion_text,
        requested_by=str(current_user.id),
    )
    return SuggestionResponse.model_validate(suggestion)


@router.post("/{strategy_id}/suggestions/{suggestion_id}/review")
async def review_suggestion_endpoint(
    strategy_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> SuggestionResponse:
    suggestion = await suggestions_orch.review_suggestion(db, suggestion_id)
    return SuggestionResponse.model_validate(suggestion)


@router.post("/{strategy_id}/suggestions/{suggestion_id}/regenerate")
async def regenerate_suggestion_endpoint(
    strategy_id: uuid.UUID,
    suggestion_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> SuggestionResponse:
    await suggestions_orch.regenerate_from_suggestion(
        db, suggestion_id, created_by=str(current_user.id)
    )
    suggestion = await db.get(StrategySuggestion, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suggestion not found")
    return SuggestionResponse.model_validate(suggestion)


@router.post("/{strategy_id}/request-promotion", status_code=status.HTTP_201_CREATED)
async def request_promotion_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> ApprovalRequestResponse:
    request = await create_approval_request(
        db,
        subject_type="strategy",
        subject_id=str(strategy_id),
        transition_type=PROMOTION_TRANSITION_TYPE,
        requested_by=str(current_user.id),
    )
    return ApprovalRequestResponse.model_validate(request)


@router.post("/{strategy_id}/promote")
async def promote_strategy_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> StrategyResponse:
    try:
        applied = await strategies_orch.promote_to_paper_trading(db, strategy_id)
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CorrelationConstraintBreachedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not applied:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Strategy is not in Backtesting status (or does not exist)",
        )
    return await _load_strategy_response(db, strategy_id)
