"""Strategy pipeline API routes (Build Spec §9). Phase 4 acceptance:
submitting an objective produces a generated strategy that passes static
validation, runs inside the sandbox, and cannot reach Backtesting ->
PaperTrading without a human approval (src/api/routes/approvals.py decides
the ApprovalRequest this flow's /promote depends on).
"""

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.paging import set_total_count
from src.api.routes.memory import memory_service_dependency
from src.api.schemas import (
    ApprovalRequestResponse,
    ApproveLiveEligibilityRequest,
    CreateStrategyRequest,
    GoLiveEvidenceResponse,
    GoLiveInputsResponse,
    GoLiveThresholdsResponse,
    NotDerivedResponse,
    StrategyResponse,
    StrategySummaryResponse,
    StrategyVersionResponse,
    StrategyVersionSummaryResponse,
    SubmitSuggestionRequest,
    SuggestionResponse,
)
from src.audit.service import write_audit_entry
from src.core.db import get_db
from src.core.rbac import Role, register_policy, require_role
from src.engine.risk.go_live_gate import (
    DEFAULT_MAX_WIN_RATE_DIVERGENCE_PP,
    DEFAULT_MIN_CALENDAR_DAYS,
    DEFAULT_MIN_CLEAN_SHADOW_DAYS,
    DEFAULT_MIN_TRADES,
    GoLiveReadinessInput,
)
from src.memory.service import MemoryService, get_pipeline_memory
from src.models.strategy import Strategy
from src.models.strategy_suggestion import StrategySuggestion
from src.models.strategy_version import StrategyVersion
from src.models.user import User
from src.orchestration import strategies as strategies_orch
from src.orchestration import strategy_suggestions as suggestions_orch
from src.orchestration.approvals import create_approval_request
from src.orchestration.go_live_evidence import GoLiveEvidence, derive_go_live_evidence
from src.orchestration.strategies import (
    LIVE_ELIGIBILITY_TRANSITION_TYPE,
    PROMOTION_TRANSITION_TYPE,
    CorrelationConstraintBreachedError,
    GoLiveGateNotPassedError,
)
from src.orchestration.transitions import ApprovalRequiredError

router = APIRouter(prefix="/strategies", tags=["strategies"])

_OPERATOR_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.PORTFOLIO_MANAGER]
# Live-eligibility sign-off is a real-money gate, not an ordinary pipeline
# promotion -- same stricter pair kill-switch reset and dual-control
# risk-limit changes use (src/api/routes/kill_switch.py,
# src/api/routes/risk_limits.py), not the broader _OPERATOR_ROLES above.
_LIVE_SIGNOFF_ROLES = [Role.SYSTEM_ADMINISTRATOR, Role.RISK_MANAGER]

register_policy("POST", "/api/v1/strategies", roles=_OPERATOR_ROLES)
register_policy("GET", "/api/v1/strategies", roles=list(Role))
register_policy("GET", "/api/v1/strategies/{strategy_id}", roles=list(Role))
register_policy("GET", "/api/v1/strategies/{strategy_id}/go-live-evidence", roles=list(Role))
register_policy("GET", "/api/v1/strategies/{strategy_id}/suggestions", roles=list(Role))
# Submitting a suggestion writes a row and feeds the regeneration flow, so it is an operator
# action like its review and regenerate steps; it was open to every role, the read-only
# auditor included.
register_policy("POST", "/api/v1/strategies/{strategy_id}/suggestions", roles=_OPERATOR_ROLES)
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
register_policy(
    "POST", "/api/v1/strategies/{strategy_id}/request-live-eligibility", roles=_LIVE_SIGNOFF_ROLES
)
register_policy(
    "POST", "/api/v1/strategies/{strategy_id}/approve-live-eligibility", roles=_LIVE_SIGNOFF_ROLES
)


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
    memory_service: MemoryService = Depends(memory_service_dependency),
) -> StrategyResponse:
    strategy, _version = await strategies_orch.run_strategy_pipeline(
        db,
        name=body.name,
        objective=body.objective,
        instrument_class=body.instrument_class,
        created_by=str(current_user.id),
        memory=get_pipeline_memory(memory_service),
    )
    return await _load_strategy_response(db, strategy.id)


@router.get("")
async def list_strategies_endpoint(
    response: Response,
    limit: int = Query(default=200, gt=0, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, max_length=32),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[StrategySummaryResponse]:
    """One page of strategies, newest first, each with a summary of its versions (no code: read one
    strategy, `GET /strategies/{id}`, for that); `X-Total-Count` is how many the filter matches.
    The list used to carry every version's code and read each strategy on its own."""
    conditions = []
    if status_filter is not None:
        conditions.append(Strategy.status == status_filter)
    total = (await db.execute(select(func.count(Strategy.id)).where(*conditions))).scalar_one()
    set_total_count(response, total)
    strategies = list(
        (
            await db.execute(
                select(Strategy)
                .where(*conditions)
                .order_by(Strategy.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    versions_by_strategy: dict[uuid.UUID, list[StrategyVersionSummaryResponse]] = {
        strategy.id: [] for strategy in strategies
    }
    if strategies:
        rows = await db.execute(
            select(
                StrategyVersion.strategy_id,
                StrategyVersion.id,
                StrategyVersion.version_number,
                StrategyVersion.static_validation_passed,
                StrategyVersion.sandbox_passed,
                StrategyVersion.created_at,
            )
            .where(StrategyVersion.strategy_id.in_(versions_by_strategy))
            .order_by(StrategyVersion.version_number)
        )
        for strategy_id, version_id, number, static_passed, sandbox_passed, created_at in rows:
            versions_by_strategy[strategy_id].append(
                StrategyVersionSummaryResponse(
                    id=version_id,
                    version_number=number,
                    static_validation_passed=static_passed,
                    sandbox_passed=sandbox_passed,
                    created_at=created_at,
                )
            )
    return [
        StrategySummaryResponse(
            id=strategy.id,
            name=strategy.name,
            objective=strategy.objective,
            instrument_class=strategy.instrument_class,
            status=strategy.status,
            versions=versions_by_strategy[strategy.id],
        )
        for strategy in strategies
    ]


@router.get("/{strategy_id}")
async def get_strategy_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> StrategyResponse:
    return await _load_strategy_response(db, strategy_id)


@router.get("/{strategy_id}/suggestions")
async def list_suggestions_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> list[SuggestionResponse]:
    """Every improvement suggestion made for this strategy, newest first, with the AI reviewer's
    verdict and, once regenerated, the version it produced and what changed."""
    if await db.get(Strategy, strategy_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Strategy not found")
    rows = await db.execute(
        select(StrategySuggestion)
        .where(StrategySuggestion.strategy_id == strategy_id)
        .order_by(StrategySuggestion.created_at.desc())
    )
    return [SuggestionResponse.model_validate(s) for s in rows.scalars().all()]


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
    memory_service: MemoryService = Depends(memory_service_dependency),
) -> SuggestionResponse:
    await suggestions_orch.regenerate_from_suggestion(
        db,
        suggestion_id,
        created_by=str(current_user.id),
        memory=get_pipeline_memory(memory_service),
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


@router.post("/{strategy_id}/request-live-eligibility", status_code=status.HTTP_201_CREATED)
async def request_live_eligibility_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> ApprovalRequestResponse:
    request = await create_approval_request(
        db,
        subject_type="strategy",
        subject_id=str(strategy_id),
        transition_type=LIVE_ELIGIBILITY_TRANSITION_TYPE,
        requested_by=str(current_user.id),
    )
    return ApprovalRequestResponse.model_validate(request)


def _evidence_response(evidence: GoLiveEvidence) -> GoLiveEvidenceResponse:
    result = evidence.readiness()
    return GoLiveEvidenceResponse(
        strategy_id=evidence.strategy_id,
        as_of=evidence.as_of.isoformat(),
        inputs=GoLiveInputsResponse(
            num_trades=evidence.inputs.num_trades,
            calendar_days_running=evidence.inputs.calendar_days_running,
            clean_shadow_mode_streak_days=evidence.inputs.clean_shadow_mode_streak_days,
            live_win_rate=evidence.inputs.live_win_rate,
            backtest_win_rate=evidence.inputs.backtest_win_rate,
        ),
        thresholds=GoLiveThresholdsResponse(
            min_trades=DEFAULT_MIN_TRADES,
            min_calendar_days=DEFAULT_MIN_CALENDAR_DAYS,
            min_clean_shadow_days=DEFAULT_MIN_CLEAN_SHADOW_DAYS,
            max_win_rate_divergence_pp=DEFAULT_MAX_WIN_RATE_DIVERGENCE_PP,
        ),
        eligible=result.eligible,
        checks=result.checks,
        reasons=result.reasons,
        counted_closing_fills=evidence.counted_closing_fills,
        counted_fills=evidence.counted_fills,
        excluded_unreal_fills=evidence.excluded_unreal_fills,
        excluded_administrative_fills=evidence.excluded_administrative_fills,
        first_counted_fill_at=(
            evidence.first_counted_fill_at.isoformat() if evidence.first_counted_fill_at else None
        ),
        backtest_run_id=evidence.backtest_run_id,
        backtest_data_source=evidence.backtest_data_source,
        shadow_clean_days_real_sandbox=evidence.shadow_clean_days_real_sandbox,
        shadow_runs_failed_recently=evidence.shadow_runs_failed_recently,
        not_derived=[
            NotDerivedResponse(input=n.input, reason=n.reason) for n in evidence.not_derived
        ],
    )


@router.get("/{strategy_id}/go-live-evidence")
async def go_live_evidence_endpoint(
    strategy_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(require_role),
) -> GoLiveEvidenceResponse:
    if await db.get(Strategy, strategy_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no strategy {strategy_id}")
    return _evidence_response(await derive_go_live_evidence(db, strategy_id))


@router.post("/{strategy_id}/approve-live-eligibility")
async def approve_live_eligibility_endpoint(
    strategy_id: uuid.UUID,
    body: ApproveLiveEligibilityRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role),
) -> StrategyResponse:
    """Sign a strategy off for live eligibility. The gate is judged on the evidence the system
    derived from its own records. A person can instead supply the numbers, but only with a reason,
    and the audit trail then holds the supplied figures beside the derived ones."""
    if await db.get(Strategy, strategy_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no strategy {strategy_id}")
    evidence = await derive_go_live_evidence(db, strategy_id)
    override = body.override if body else None
    if override is None:
        readiness_input = evidence.inputs
    else:
        readiness_input = GoLiveReadinessInput(
            num_trades=override.num_trades,
            calendar_days_running=override.calendar_days_running,
            clean_shadow_mode_streak_days=override.clean_shadow_mode_streak_days,
            live_win_rate=override.live_win_rate,
            backtest_win_rate=override.backtest_win_rate,
        )
    try:
        applied = await strategies_orch.approve_strategy_for_live_eligibility(
            db, strategy_id, readiness_input=readiness_input
        )
    except ApprovalRequiredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except GoLiveGateNotPassedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if not applied:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Strategy is not in PaperTrading status (or does not exist)",
        )
    if override is not None:
        await write_audit_entry(
            db,
            actor=current_user.email,
            action="strategy.go_live_evidence_overridden",
            entity_type="strategy",
            entity_id=str(strategy_id),
            details={
                "reason": override.reason,
                "supplied": asdict(readiness_input),
                "derived": asdict(evidence.inputs),
                "derived_eligible": evidence.readiness().eligible,
                "not_derived": [n.input for n in evidence.not_derived],
            },
        )
        await db.commit()
    return await _load_strategy_response(db, strategy_id)
