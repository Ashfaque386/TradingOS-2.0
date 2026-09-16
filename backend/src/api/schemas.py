import uuid
from datetime import date as date_type
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from src.core.roles import Role
from src.models.organization_run import FailureClass, RunSource, RunStatus, RunType
from src.models.task import TaskStatus


class UserRegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    role: Role
    is_active: bool


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPairResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class CreateRunRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)


class TaskSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    plan_key: str
    capability: str
    name: str
    status: TaskStatus
    failure_class: FailureClass | None
    last_error: str | None


class RunResponse(BaseModel):
    id: uuid.UUID
    objective: str
    source: RunSource
    run_type: RunType
    status: RunStatus
    failure_class: FailureClass | None
    source_run_id: uuid.UUID | None
    error: str | None
    tasks: list[TaskSummaryResponse] = []


class AgentSummaryResponse(BaseModel):
    agent_id: str
    display_name: str
    department: str
    can_disable: bool
    enabled: bool
    capabilities: list[str]
    skills: list[str]
    heartbeat_enabled: bool


class RunPipelineRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)


class RunPipelineResponse(BaseModel):
    objective: str
    node_log: list[str]
    deployment_result: dict | None
    evaluation_verdict: dict | None
    rejection_count: int


class CreateStrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    objective: str = Field(min_length=1, max_length=4000)
    instrument_class: str = "equity"


class StrategyVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_number: int
    static_validation_passed: bool
    static_validation_errors: list[str] | None
    sandbox_passed: bool | None
    sandbox_result: dict | None
    options_legs: dict | None


class StrategyResponse(BaseModel):
    id: uuid.UUID
    name: str
    objective: str
    instrument_class: str
    status: str
    versions: list[StrategyVersionResponse] = []


class SubmitSuggestionRequest(BaseModel):
    base_version_id: uuid.UUID
    suggestion_text: str = Field(min_length=1, max_length=4000)


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    strategy_id: uuid.UUID
    base_version_id: uuid.UUID
    suggestion_text: str
    status: str
    ai_verdict: dict | None
    regenerated_version_id: uuid.UUID | None
    regeneration_diff: str | None


class DecideApprovalRequest(BaseModel):
    approve: bool
    reason: str | None = None


class ApprovalRequestResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subject_type: str
    subject_id: str
    transition_type: str
    status: str
    requested_by: str | None
    decided_by: str | None


# --- Backtesting & Optimization (Build Spec §10) ---------------------------
#
# Strategy/signal logic accepted over HTTP is limited to a small fixed set
# of built-in generators (BuiltinStrategy) -- the API never accepts a
# callable or expression from the request body, the same "no arbitrary
# code over this surface" posture Phase 4's sandbox exists for elsewhere.

BuiltinStrategy = Literal["always_long", "sma_crossover"]


class OHLCVBar(BaseModel):
    date: date_type
    open: float
    high: float
    low: float
    close: float
    volume: float


class RunBacktestRequest(BaseModel):
    strategy_version_id: uuid.UUID
    symbol: str = Field(min_length=1, max_length=32)
    bars: list[OHLCVBar] = Field(min_length=2)
    strategy: BuiltinStrategy = "sma_crossover"
    sma_window: int = Field(default=15, gt=0)
    as_of: date_type
    is_delivery: bool = True
    initial_capital: float = Field(default=100_000.0, gt=0)


class BacktestRunResponse(BaseModel):
    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    status: str
    refusal_reason: str | None
    metrics: dict | None
    walk_forward_result: dict | None
    monte_carlo_result: dict | None


class WalkForwardRequest(BaseModel):
    bars: list[OHLCVBar] = Field(min_length=2)
    train_bars: int = Field(gt=0)
    test_bars: int = Field(gt=0)
    step_bars: int | None = Field(default=None, gt=0)
    strategy: BuiltinStrategy = "sma_crossover"
    sma_window: int = Field(default=15, gt=0)


class MonteCarloRequest(BaseModel):
    n_paths: int = Field(default=10_000, gt=0)
    seed: int | None = None


class CompareRunsRequest(BaseModel):
    run_ids: list[uuid.UUID] = Field(min_length=2)


class ComparisonResponse(BaseModel):
    run_ids: list[str]
    # Keyed "<run_id_a>|<run_id_b>" (sorted) -> correlation, or null when
    # fewer than 10 overlapping days (src.engine.backtest.comparison).
    correlations: dict[str, float | None]


class OptimizeRequest(BaseModel):
    strategy_version_id: uuid.UUID
    bars: list[OHLCVBar] = Field(min_length=2)
    n_trials: int = Field(default=30, gt=0, le=200)
    objective_metric: Literal["sharpe", "total_return", "cagr"] = "sharpe"
    sma_window_min: int = Field(default=5, gt=0)
    sma_window_max: int = Field(default=50, gt=0)
    seed: int | None = None


class OptimizationRunResponse(BaseModel):
    id: uuid.UUID
    n_trials: int
    best_params: dict | None
    best_value: float | None
    param_importance: dict | None


KillSwitchMode = Literal["live", "paper"]


class KillSwitchStateResponse(BaseModel):
    mode: str
    tripped: bool
    trip_reason: str | None
    last_drawdown_pct: float | None
    threshold_pct: float


class CheckDrawdownRequest(BaseModel):
    current_equity: float = Field(gt=0)
    peak_equity: float = Field(gt=0)
    threshold_pct: float | None = Field(default=None, gt=0)


class ResetKillSwitchRequest(BaseModel):
    reason: str | None = None


class StageRiskLimitChangeRequest(BaseModel):
    limit_name: str = Field(min_length=1, max_length=64)
    proposed_value: float
    reason: str | None = None


class RiskLimitChangeResponse(BaseModel):
    id: uuid.UUID
    limit_name: str
    proposed_value: float
    status: str
    staged_by: str
    confirmed_by: str | None
    applied_by: str | None


class RiskLimitResponse(BaseModel):
    name: str
    value: float


class GoLiveReadinessRequest(BaseModel):
    num_trades: int = Field(ge=0)
    calendar_days_running: int = Field(ge=0)
    clean_shadow_mode_streak_days: int = Field(ge=0)
    live_win_rate: float | None = Field(default=None, ge=0, le=1)
    backtest_win_rate: float | None = Field(default=None, ge=0, le=1)
    min_trades: int = Field(default=30, gt=0)
    min_calendar_days: int = Field(default=21, gt=0)
    min_clean_shadow_days: int = Field(default=10, gt=0)
    max_win_rate_divergence_pp: float = Field(default=20.0, gt=0)


class GoLiveReadinessResponse(BaseModel):
    eligible: bool
    checks: dict[str, bool]
    reasons: list[str]


class OrderIntentRequest(BaseModel):
    mode: KillSwitchMode
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    proposed_price: float = Field(gt=0)
    reference_price: float = Field(gt=0)
    proposed_position_value: float = Field(gt=0)
    portfolio_value: float = Field(gt=0)


class OrderIntentResponse(BaseModel):
    mode: str
    symbol: str
    side: str
    quantity: int
    price: float
    created_at: str
