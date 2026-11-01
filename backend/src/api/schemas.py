import uuid
from datetime import date as date_type
from datetime import datetime as datetime_type
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from src.core.roles import Role
from src.engine.backtest.signals import BuiltinStrategy as EngineBuiltinStrategy
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


class AdminCreateUserRequest(BaseModel):
    """Phase 21 (docs/phase20-old-vs-new-comparison.md item 10):
    SystemAdministrator-only, unlike `UserRegisterRequest` above -- the
    only way to create a user at any role other than the self-registration
    bootstrap-admin-or-ReadOnlyAuditor rule `POST /auth/register` enforces."""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    role: Role


class AdminChangeRoleRequest(BaseModel):
    role: Role


class AdminRevokeSessionsResponse(BaseModel):
    user_id: uuid.UUID
    revoked_count: int


class JwtSigningKeyRotateResponse(BaseModel):
    """Never echoes the new key itself -- same "secrets are write-only,
    never returned in a response" posture as every credential store in
    this codebase."""

    rotated_at: str
    rotated_by: str
    sessions_revoked: int


class JwtSigningKeyStatusResponse(BaseModel):
    """Where the signing key came from and whether it can be trusted -- never
    the key itself."""

    created_at: str
    created_by: str
    weak: bool
    weakness: str | None


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
    created_at: datetime_type
    started_at: datetime_type | None
    completed_at: datetime_type | None
    tasks: list[TaskSummaryResponse] = []


class AgentSummaryResponse(BaseModel):
    """`identity_name`/`emoji`/`avatar`/`theme`/`voice` are the *effective*
    identity (`EffectiveAgentConfig`, src.gateway.loader) -- `identity_name`
    always falls back to the fixed roster's own `display_name` when no
    override exists, matching `display_name` exactly in that case; the
    other four are genuinely `None` until an operator sets one via `PUT
    /api/v1/agents/{agent_id}/identity`, never a fabricated default."""

    agent_id: str
    display_name: str
    department: str
    can_disable: bool
    enabled: bool
    capabilities: list[str]
    skills: list[str]
    heartbeat_enabled: bool
    identity_name: str
    emoji: str | None
    avatar: str | None
    theme: str | None
    voice: str | None
    # Phase 19 (docs/phase19-audit.md §3.2): False only for the 3 agents
    # whose read capability names a real thing (fundamentals, valuation,
    # macro/economic calendar) no data source in this codebase provides --
    # see src.agents.roster.NO_DATA_SOURCE_AGENTS. Every other agent is
    # True, including ones with no pipeline node at all (data-ingestion,
    # notification, etc.), since "no data source" here specifically means
    # "this agent's own subject matter has no backing data anywhere," not
    # "this agent isn't wired to the LangGraph pipeline."
    has_data_source: bool


class RunPipelineRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=4000)


class RunPipelineResponse(BaseModel):
    objective: str
    node_log: list[str]
    deployment_result: dict[str, Any] | None
    evaluation_verdict: dict[str, Any] | None
    rejection_count: int
    # Phase 19 Finding #1: the id this run's live step events were recorded
    # and published under -- feed it to GET /agents/pipeline/events or the
    # /ws/agent-pipeline-events `run_id` filter to follow or replay the run.
    run_id: str
    # True when the run was cancelled (at a step boundary or while a step was
    # running -- that step is abandoned): `node_log` and the other fields then
    # describe only the steps that finished before it stopped, and nothing was
    # recorded to agent memory for it.
    cancelled: bool = False


class CancelPipelineRunResponse(BaseModel):
    """Cancelling a pipeline run is a request the run notices within a moment
    (it abandons the step in progress), not an instant stop -- so the answer is
    'accepted', not 'done'.
    The run's own terminal event (`pipeline.cancelled`, or `pipeline.completed`
    if it finished first) says what actually happened."""

    run_id: str
    status: Literal["cancel_requested"] = "cancel_requested"


class AgentPipelineEventResponse(BaseModel):
    run_id: str
    sequence: int
    event_type: str
    node: str | None
    agent_id: str | None
    payload: dict[str, Any] | None
    created_at: str


class SetAgentIdentityRequest(BaseModel):
    """Every field optional -- only what's actually being changed needs to
    be sent, matching `src.gateway.service.set_identity`'s own
    only-overwrite-what's-given behavior (a field left out keeps its
    current value, it is never reset to `None`)."""

    name: str | None = Field(default=None, min_length=1)
    emoji: str | None = None
    avatar: str | None = None
    theme: str | None = None
    voice: str | None = None


class PromptVersionResponse(BaseModel):
    id: uuid.UUID
    agent_id: str
    version_number: int
    content: str
    status: str
    diff_from_previous: str | None
    created_by: str | None
    created_at: str
    activated_at: str | None


class CreatePromptVersionRequest(BaseModel):
    content: str = Field(min_length=1)


class AgentHeartbeatEntryResponse(BaseModel):
    status: str
    details: dict[str, Any] | None
    checked_at: str


class AgentTaskEntryResponse(BaseModel):
    """A real orchestration Task (src.models.task.Task) whose `capability`
    is one this agent is registered for (src.agents.roster.capabilities_for)
    -- the only per-agent execution-outcome signal this codebase has for a
    pipeline-node agent, since Task itself carries no agent_id column."""

    id: uuid.UUID
    run_id: uuid.UUID
    name: str
    status: str
    last_error: str | None
    created_at: str
    completed_at: str | None


class AgentActivityResponse(BaseModel):
    """Phase 19 (docs/phase19-audit.md Part 2.2/3.2): the audit's own
    finding was that Agent Fleet had no per-agent activity/performance
    history anywhere. Both lists here are real rows scoped to this one
    agent, never fabricated -- `heartbeats` is empty for any agent whose
    heartbeat is disabled (see AgentSummaryResponse.heartbeat_enabled),
    and `tasks` is empty for any agent with no capability ever claimed by
    the task engine."""

    agent_id: str
    heartbeats: list[AgentHeartbeatEntryResponse]
    tasks: list[AgentTaskEntryResponse]


class AgentAnalyticsSummaryRow(BaseModel):
    """Phase 22 (docs/phase20-old-vs-new-comparison.md item 21): one row
    per roster agent, real counts over `Task` rows joined by capability
    (the same join Phase 19's `/activity` endpoint already established --
    `Task` has no `agent_id` column). `avg_duration_seconds` only counts
    terminal (succeeded/failed) tasks that actually have both a
    `created_at` and `completed_at` -- never averaged against a still-open
    task, which would understate every agent's real duration."""

    agent_id: str
    display_name: str
    tasks_total: int
    tasks_succeeded: int
    tasks_failed: int
    # None when this agent has claimed zero tasks -- an honest "no data",
    # never a fabricated 0% that would read as "always fails".
    success_rate: float | None
    avg_duration_seconds: float | None


class AgentAnalyticsTrendPoint(BaseModel):
    """One real day's real task outcomes, system-wide -- deliberately not
    split per-agent (a 30-line chart is unreadable); per-agent detail is
    what `/agents/{id}/activity` is for."""

    date: str
    tasks_succeeded: int
    tasks_failed: int


class CreateStrategyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    objective: str = Field(min_length=1, max_length=4000)
    instrument_class: str = "equity"


class StrategyVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    version_number: int
    code: str
    static_validation_passed: bool
    static_validation_errors: list[str] | None
    sandbox_passed: bool | None
    sandbox_result: dict[str, Any] | None
    options_legs: dict[str, Any] | None
    created_at: datetime_type


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
    ai_verdict: dict[str, Any] | None
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
    reason: str | None
    decided_by: str | None
    decided_at: datetime_type | None
    # What the decider wrote when they approved or rejected (`reason` is why it was asked for).
    decision_reason: str | None = None
    created_at: datetime_type
    # What the ids stand for, where known: the strategy's name, and the people by e-mail.
    subject_label: str | None = None
    requested_by_label: str | None = None
    decided_by_label: str | None = None


class PortfolioAllocationEntry(BaseModel):
    strategy_id: str
    strategy_name: str
    strategy_status: str
    action: str
    rationale: str
    sharpe: float | None
    max_drawdown: float | None


class PortfolioRecommendationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    summary: str
    allocations: list[PortfolioAllocationEntry]
    based_on: dict[str, Any]
    llm_source: str
    status: str
    reviewed_by: str | None
    reviewed_at: datetime_type | None
    reviewer_notes: str | None
    created_at: datetime_type


class DecidePortfolioRecommendationRequest(BaseModel):
    notes: str | None = None


class PortfolioSummaryResponse(BaseModel):
    as_of: str
    active_strategy_count: int
    paper_realized_pnl_today: float
    paper_costs_today: float = 0.0
    paper_open_position_count: int
    # How many of those sit on an inactive subscription (no stop-loss, no exits).
    paper_unmanaged_position_count: int = 0
    live_position_count: int
    broker_configured: bool
    broker_name: str | None
    available_margin: float | None
    used_margin: float | None


class PortfolioExposureEntry(BaseModel):
    mode: str
    strategy_id: str
    strategy_name: str
    symbol: str
    exposure: float


class PortfolioRiskMetricsResponse(BaseModel):
    as_of: str
    open_position_count: int
    total_exposure: float
    exposure_by_position: list[PortfolioExposureEntry]
    largest_position_concentration_pct: float | None
    broker_configured: bool
    margin_utilization_pct: float | None


# --- Backtesting & Optimization (Build Spec §10) ---------------------------
#
# Strategy/signal logic accepted over HTTP is limited to a small fixed set
# of built-in generators (BuiltinStrategy) -- the API never accepts a
# callable or expression from the request body, the same "no arbitrary
# code over this surface" posture Phase 4's sandbox exists for elsewhere.
# The canonical type lives in src.engine.backtest.signals (shared with
# Phase 7's daily signal layer); re-exported here for the HTTP schemas.

BuiltinStrategy = EngineBuiltinStrategy


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
    start_date: date_type
    end_date: date_type
    status: str
    refusal_reason: str | None
    metrics: dict[str, Any] | None
    daily_returns: list[Any] | None
    trade_pnls: list[float] | None
    walk_forward_result: dict[str, Any] | None
    monte_carlo_result: dict[str, Any] | None
    # Where the bars came from (see src.models.data_source): a run posted by a caller is
    # `supplied` -- this system cannot say whether the bars were real.
    data_source: str = "unknown"
    created_at: datetime_type


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
    best_params: dict[str, Any] | None
    best_value: float | None
    param_importance: dict[str, Any] | None


KillSwitchMode = Literal["live", "paper"]


class KillSwitchStateResponse(BaseModel):
    mode: str
    tripped: bool
    trip_reason: str | None
    last_drawdown_pct: float | None
    threshold_pct: float
    tripped_at: str | None = None
    reset_at: str | None = None
    reset_reason: str | None = None


class CheckDrawdownRequest(BaseModel):
    current_equity: float = Field(gt=0)
    peak_equity: float = Field(gt=0)
    threshold_pct: float | None = Field(default=None, gt=0)


class ResetKillSwitchRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class StageRiskLimitChangeRequest(BaseModel):
    limit_name: str = Field(min_length=1, max_length=64)
    proposed_value: float
    reason: str | None = None


class RiskLimitChangeResponse(BaseModel):
    id: uuid.UUID
    limit_name: str
    proposed_value: float
    reason: str | None
    status: str
    staged_by: str
    staged_at: datetime_type
    confirmed_by: str | None
    confirmed_at: datetime_type | None
    applied_by: str | None
    applied_at: datetime_type | None
    rejected_by: str | None = None
    rejected_at: datetime_type | None = None
    rejection_reason: str | None = None
    # `staged_by` and the rest are user ids; these are the same people by e-mail, where known.
    staged_by_label: str | None = None
    confirmed_by_label: str | None = None
    applied_by_label: str | None = None
    rejected_by_label: str | None = None


class RiskLimitResponse(BaseModel):
    name: str
    value: float


class RejectRiskLimitChangeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class RiskLimitCatalogEntry(BaseModel):
    name: str
    label: str
    description: str
    unit: str
    default: float
    minimum: float
    maximum: float
    enforced: bool
    # What is in force now: the applied value, or the default when none was ever applied.
    effective: float
    applied: bool


class EquitySnapshotResponse(BaseModel):
    taken_at: datetime_type
    equity: float | None
    capital_base: float | None
    realized_net: float | None
    unrealized: float | None
    open_positions: int
    unpriced_positions: int
    price_source: str
    peak_equity: float | None
    drawdown_pct: float | None
    note: str | None


class EquityHistoryResponse(BaseModel):
    mode: str
    # Oldest first. A reading that could not be measured has `equity: null` and a note.
    snapshots: list[EquitySnapshotResponse]
    threshold_pct: float


class EvaluateKillSwitchResponse(BaseModel):
    # ok | tripped | already_tripped | cannot_evaluate
    outcome: str
    snapshot: EquitySnapshotResponse
    kill_switch: KillSwitchStateResponse


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


class GoLiveInputsResponse(BaseModel):
    num_trades: int
    calendar_days_running: int
    clean_shadow_mode_streak_days: int
    live_win_rate: float | None
    backtest_win_rate: float | None


class GoLiveThresholdsResponse(BaseModel):
    min_trades: int
    min_calendar_days: int
    min_clean_shadow_days: int
    max_win_rate_divergence_pp: float


class NotDerivedResponse(BaseModel):
    input: str
    reason: str


class GoLiveEvidenceResponse(BaseModel):
    """The Go-Live inputs as the system worked them out from its own records, judged by the gate,
    with what was counted, what was set aside and what could not be derived."""

    strategy_id: uuid.UUID
    as_of: str
    inputs: GoLiveInputsResponse
    thresholds: GoLiveThresholdsResponse
    eligible: bool
    checks: dict[str, bool]
    reasons: list[str]
    counted_closing_fills: int
    counted_fills: int
    excluded_unreal_fills: int
    excluded_administrative_fills: int
    first_counted_fill_at: str | None
    backtest_run_id: uuid.UUID | None
    backtest_data_source: str | None
    shadow_clean_days_real_sandbox: int
    shadow_runs_failed_recently: int
    not_derived: list[NotDerivedResponse]


class GoLiveOverride(BaseModel):
    """Numbers a person puts in place of the derived evidence. Allowed, but never silent: the
    reason is required, and the audit trail keeps what was supplied beside what the records said."""

    num_trades: int = Field(ge=0)
    calendar_days_running: int = Field(ge=0)
    clean_shadow_mode_streak_days: int = Field(ge=0)
    live_win_rate: float | None = Field(default=None, ge=0, le=1)
    backtest_win_rate: float | None = Field(default=None, ge=0, le=1)
    reason: str = Field(min_length=10, max_length=500)


class ApproveLiveEligibilityRequest(BaseModel):
    # Absent: the gate is judged on the derived evidence.
    override: GoLiveOverride | None = None


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


# --- Paper Trading Engine (Build Spec §11) ----------------------------------


class EnrollPaperTradingRequest(BaseModel):
    strategy_version_id: uuid.UUID
    symbol: str = Field(min_length=1, max_length=32)
    builtin_strategy: EngineBuiltinStrategy = "sma_crossover"
    sma_window: int = Field(default=15, gt=0)
    initial_capital: float = Field(default=100_000.0, gt=0)
    # Unset: 3% for equity and futures, 50% of the premium for an option strategy (3% of a premium
    # is noise).
    stop_loss_pct: float | None = Field(default=None, gt=0)
    position_size_pct: float = Field(default=5.0, gt=0, le=100)
    # `future`: `symbol` is the underlying (NIFTY, RELIANCE); the monthly contract and its lot size
    # come from the connected broker's instrument master, and entries are `lots` whole lots
    # (position_size_pct does not apply to a future). `option`: `symbol` is the underlying, and
    # `option_strategy` chooses the legs (long_option: the at-the-money call on BUY, put on SELL;
    # vertical_spread: the same, hedged by a sold leg `spread_width` strikes further out).
    instrument_kind: Literal["equity", "future", "option"] = "equity"
    lots: int = Field(default=1, ge=1, le=1000)
    option_strategy: Literal["long_option", "vertical_spread"] = "long_option"
    spread_width: int = Field(default=2, ge=1, le=20)


class EnrolPaperPreviewRequest(EnrollPaperTradingRequest):
    """The same fields as an enrolment, so a preview is of exactly what would be sent; the strategy
    version is optional (given, it also checks for an active duplicate)."""

    strategy_version_id: uuid.UUID | None = None  # type: ignore[assignment]


class PaperSymbolHitResponse(BaseModel):
    symbol: str
    kind: Literal["equity", "index"]


class PaperSymbolSearchResponse(BaseModel):
    # False when there is nothing to search (no broker connected): the caller types a symbol.
    available: bool
    reason: str | None = None
    symbols: list[PaperSymbolHitResponse] = []


class PaperOptionLegPreview(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    option_type: Literal["CE", "PE"]
    strike: float
    quantity: int
    price: float | None


class PaperOptionScenarioPreview(BaseModel):
    signal: Literal["BUY", "SELL"]
    view: str
    legs: list[PaperOptionLegPreview]
    max_loss: float | None
    fits: bool | None
    note: str | None = None


class PaperEnrolPreviewResponse(BaseModel):
    symbol: str
    instrument_kind: Literal["equity", "future", "option"]
    listing: Literal["listed", "not_listed", "unknown"]
    price: float | None = None
    price_symbol: str | None = None
    equity: dict[str, float | int | None] | None = None
    future: dict[str, float | int | str | bool | None] | None = None
    option: dict[str, float | int | str | None] | None = None
    scenarios: list[PaperOptionScenarioPreview] = []
    problems: list[str] = []
    warnings: list[str] = []


class PaperCloseLegPreview(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    quantity: int
    avg_cost: float
    reference_price: float | None = None
    reference_age_seconds: float | None = None
    reference_source: str | None = None
    book_age_seconds: float | None = None
    book_source: str | None = None
    est_avg_price: float | None = None
    est_filled: int = 0
    est_remaining: int = 0
    est_costs: float | None = None
    est_gross_pnl: float | None = None
    problem: str | None = None


class PaperClosePreviewResponse(BaseModel):
    subscription_id: uuid.UUID
    symbol: str
    market_open: bool
    legs: list[PaperCloseLegPreview]
    blockers: list[str] = []
    notes: list[str] = []
    est_costs_total: float | None = None
    est_gross_pnl: float | None = None
    can_close: bool


class UpdatePaperSettingsRequest(BaseModel):
    """Only the fields sent are changed; a field left out (or null) is left alone. The bounds are
    the enrolment's own."""

    reason: str = Field(min_length=3, max_length=500)
    stop_loss_pct: float | None = Field(default=None, gt=0)
    position_size_pct: float | None = Field(default=None, gt=0, le=100)
    lots: int | None = Field(default=None, ge=1, le=1000)
    spread_width: int | None = Field(default=None, ge=1, le=20)
    sma_window: int | None = Field(default=None, gt=0)
    builtin_strategy: EngineBuiltinStrategy | None = None
    initial_capital: float | None = Field(default=None, gt=0)


class PaperSettingChange(BaseModel):
    field: str
    before: float | int | str
    after: float | int | str


class UpdatePaperSettingsResponse(BaseModel):
    subscription: "PaperTradingSubscriptionResponse"
    changes: list[PaperSettingChange]


class PaperTradingSubscriptionResponse(BaseModel):
    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    builtin_strategy: str
    sma_window: int
    initial_capital: float
    stop_loss_pct: float
    position_size_pct: float
    is_active: bool
    instrument_kind: Literal["equity", "future", "option"] = "equity"
    contract_symbol: str | None = None
    contract_expiry: str | None = None
    lot_size: int | None = None
    lots: int = 1
    option_strategy: str | None = None
    spread_width: int = 2


class PaperPositionResponse(BaseModel):
    subscription_id: uuid.UUID
    symbol: str
    quantity: int
    avg_cost: float
    # Gross of costs (the price difference only); `costs_total` is what every fill so far cost
    # (brokerage, STT, exchange charges, SEBI fee, stamp duty, GST) and `net_realized_pnl` the
    # difference.
    realized_pnl: float
    costs_total: float = 0.0
    net_realized_pnl: float = 0.0
    # Only on `GET .../positions`, and only for an open position: the last price published for
    # this contract, how old it is, and the unrealized P&L off it. `None` (not 0) when it has
    # never been priced -- an unpriced leg's value is unknown, not zero.
    mark_price: float | None = None
    mark_age_seconds: float | None = None
    unrealized_pnl: float | None = None


class PaperLastTick(BaseModel):
    price: float
    age_seconds: float
    source: str
    # When the exchange stamped this price and how old that stamp is now: null where the broker
    # sends none (Upstox's is not yet verified), which is "not known", not "fresh".
    exchange_time: datetime_type | None = None
    exchange_age_seconds: float | None = None
    # The exchange's circuit limits for the day from the same quote; null when it carried none.
    lower_circuit: float | None = None
    upper_circuit: float | None = None


class PaperQuoteRefusal(BaseModel):
    """The newest quote for this subscription was not acted on, and why (its exchange stamp was
    from a previous day, or older than PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS). Present only while
    the refusals are continuing."""

    broker: str
    reason: str
    exchange_time: datetime_type | None = None
    at: datetime_type


class PaperSubscriptionDetailResponse(BaseModel):
    """One row of the subscription list: the subscription, what state it is in and why, and its
    position. `state` is derived: `inactive`; else `awaiting_history` (too little real price
    history to signal); else `halted_no_data` (no real price feed); else `active`.
    `managed` is false for an open position on an inactive subscription: nothing watches its
    stop-loss."""

    id: uuid.UUID
    strategy_version_id: uuid.UUID
    symbol: str
    builtin_strategy: str
    sma_window: int
    initial_capital: float
    stop_loss_pct: float
    position_size_pct: float
    is_active: bool
    instrument_kind: Literal["equity", "future", "option"] = "equity"
    contract_symbol: str | None = None
    contract_expiry: str | None = None
    lot_size: int | None = None
    lots: int = 1
    option_strategy: str | None = None
    spread_width: int = 2
    # Positions that are not flat: 0 or 1 for equity and futures, one per leg for an option
    # strategy (whose `quantity` and `avg_cost` are 0 across legs: see GET .../positions).
    open_legs: int = 0
    state: Literal["active", "awaiting_history", "halted_no_data", "inactive"]
    state_reason: str | None
    created_at: str
    created_by: str | None
    deactivated_at: str | None
    deactivated_by: str | None
    deactivation_reason: str | None
    quantity: int
    avg_cost: float
    realized_pnl: float
    costs_total: float = 0.0
    net_realized_pnl: float = 0.0
    managed: bool
    last_tick: PaperLastTick | None
    unrealized_pnl: float | None
    quote_refusal: PaperQuoteRefusal | None = None
    # The band, as a percentage of the previous close, the risk gate falls back to when a quote
    # carries no exchange circuit limits (the reference table's: not an exchange figure).
    circuit_fallback_band_pct: float | None = None


class PaperHistoryBackfillResponse(BaseModel):
    """The outcome of fetching a subscription's real price history: how much, from which broker,
    and -- when it failed -- why."""

    status: Literal["success", "failed"]
    symbol: str
    rows: int
    first_date: str | None
    last_date: str | None
    source: str | None
    unreal_rows_removed: int
    error: str | None


class DeactivatePaperSubscriptionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    # Required when a position is open: 'close' closes it first (through the risk gate);
    # 'leave_unmanaged' accepts that nothing will watch its stop-loss afterwards.
    on_open_position: Literal["close", "leave_unmanaged"] | None = None
    close_price: float | None = Field(default=None, gt=0)


class ClosePaperPositionRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    # Close at this price (recorded as operator-supplied); omit to use a live tick no older
    # than two minutes.
    price: float | None = Field(default=None, gt=0)
    # SystemAdministrator only: skip the risk gate and fill the whole quantity at `price`.
    administrative: bool = False


class PaperPositionClosedResponse(BaseModel):
    fill: "PaperFillResponse"
    position: PaperPositionResponse
    closed: bool
    remaining_quantity: int
    price: float
    price_basis: str
    data_source: str
    administrative: bool
    # An option strategy closes every leg: `fill` and `position` are the first one's, and these
    # list every fill and say why a leg did not close.
    legs: list["PaperFillResponse"] = []
    failures: list[str] = []


class DeactivatePaperSubscriptionResponse(BaseModel):
    subscription: PaperTradingSubscriptionResponse
    closed: PaperPositionClosedResponse | None
    left_unmanaged_quantity: int


class PaperFillResponse(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    symbol: str
    side: str
    order_group_id: uuid.UUID
    leg_index: int
    requested_quantity: int
    filled_quantity: int
    avg_fill_price: float | None
    fully_filled: bool
    realized_pnl: float
    # What the fill cost, gross realized_pnl aside: the total and each line (brokerage, stt,
    # exchange_txn_charges, sebi_fee, stamp_duty, gst, plus the product and turnover they were
    # worked out for).
    costs_total: float = 0.0
    costs_breakdown: dict[str, float | str] | None = None
    # What the fill rested on (see src.models.data_source): `real` for a real tick walked through
    # the broker's real depth, `mixed` when the book was generated.
    data_source: str = "unknown"
    created_at: str


class TodaysPaperPnlResponse(BaseModel):
    as_of_date: str
    # Gross of costs; `net_realized_pnl` is after the fills' costs.
    realized_pnl: float
    costs_total: float = 0.0
    net_realized_pnl: float = 0.0
    fill_count: int


class UnrealizedPaperPnlResponse(BaseModel):
    """Mark-to-market P&L for currently-open paper positions -- not
    date-scoped, unlike `TodaysPaperPnlResponse`, since a position may
    have been opened on an earlier day and is still marked as of now.
    `price_source` is `"real"` only when a broker is actually configured
    and feeding real quotes into the tick stream, `"synthetic"` when
    `MockTickSource`'s random walk is the only source -- the two are
    never blended into one unlabeled number. `unrealized_pnl` is `0.0`
    when there are genuinely no open positions (a real, correct zero),
    but `None` when open positions exist yet none of them has a
    published tick to mark against (unknown, never fabricated as `0.0`).
    `positions_unpriced` says when the figure covers less than every open
    position."""

    as_of: str
    # "none": no broker is connected and synthetic mode is off, so nothing is feeding prices
    # (any last tick in the stream is stale, and old ones may be synthetic).
    price_source: Literal["real", "synthetic", "none"]
    unrealized_pnl: float | None
    positions_priced: int
    positions_unpriced: int
    # Open positions whose subscription is inactive: nothing is watching their stop-loss.
    unmanaged_positions: int = 0


class DailySignalResponse(BaseModel):
    id: uuid.UUID
    subscription_id: uuid.UUID
    symbol: str
    signal_type: str
    reference_price: float
    consumed: bool


class RunDailySignalRequest(BaseModel):
    as_of: date_type | None = None


class ProcessTickRequest(BaseModel):
    tick_price: float = Field(gt=0)


class WriteBrokerCredentialsRequest(BaseModel):
    """Write-only by design (Build Spec §20): there is no corresponding
    response schema that echoes any of these fields back -- see
    BrokerCredentialStatusResponse below, which only ever reports whether
    a broker is configured, never the values themselves."""

    api_key: str = Field(min_length=1)
    api_secret: str | None = None
    access_token: str | None = None
    # Optional custom OAuth callback URL, saved alongside the keys so an
    # operator can set it before any credential exists. Omitted keeps
    # whatever override was already stored.
    redirect_uri: str | None = None


class WriteBrokerRedirectUriRequest(BaseModel):
    """`null` resets to the default, request-derived callback URL."""

    redirect_uri: str | None = None


class BrokerCredentialStatusResponse(BaseModel):
    """token_status/token_expires_at/redirect_uri/token_duration are all
    OAuth metadata, never secrets -- safe to echo back, unlike
    api_key/api_secret/access_token, which this response never carries."""

    broker: str
    configured: bool
    token_status: str = "never-connected"  # "never-connected" | "valid" | "expired"
    token_expires_at: str | None = None
    # Always populated, even before any credential is saved: it has to be
    # registered in the broker's developer console *before* the broker
    # issues the API key. The custom override when set, else the default.
    redirect_uri: str | None = None
    default_redirect_uri: str | None = None
    redirect_uri_is_custom: bool = False
    token_duration: str | None = None
    # What the broker itself last said about these credentials, which the clock-derived
    # `token_status` cannot know: set while its quote feed is being refused (HTTP 401/403),
    # cleared when a call succeeds or the credentials are replaced. Null means "no
    # rejection known", not "accepted".
    feed_rejection_status: int | None = None
    feed_rejection_detail: str | None = None
    feed_rejected_at: str | None = None
    # Quotes this broker answered that were not acted on because the exchange's own stamp on them
    # was from a previous day (or too old), since the credentials last changed.
    quotes_refused_count: int = 0
    quotes_refused_last_symbol: str | None = None
    quotes_refused_last_reason: str | None = None
    quotes_refused_last_at: str | None = None


class BrokerCircuitBreakerStatusResponse(BaseModel):
    """Real, queryable state from the one persistent `BrokerCircuitBreaker`
    instance src.brokers.breaker_registry holds per broker -- `state`
    reflects genuine consecutive-5xx-failure history across every real
    caller (tick source, live-trading scheduler, API-route adapters),
    not a per-call snapshot that resets itself the instant the request
    handling it returns. `cooldown_remaining_seconds` is `None` whenever
    `state == "closed"` (there is nothing counting down)."""

    broker: str
    state: str  # "closed" | "open"
    consecutive_failures: int
    failure_threshold: int
    cooldown_remaining_seconds: float | None = None


class BrokerOAuthLoginUrlResponse(BaseModel):
    login_url: str
    redirect_uri: str


class ShadowModeCheckRequest(BaseModel):
    broker: str = Field(min_length=1)
    # The strategy whose Go-Live readiness this check counts toward.
    strategy_id: uuid.UUID | None = None
    symbol: str = Field(min_length=1, max_length=32)
    side: Literal["buy", "sell"]
    quantity: int = Field(gt=0)
    order_type: Literal["market", "limit"] = "market"
    price: float | None = Field(default=None, gt=0)
    product: str = "MIS"


class ShadowModeRunResponse(BaseModel):
    id: uuid.UUID
    broker_name: str
    has_sandbox: bool
    confidence: str
    symbol: str
    side: str
    quantity: int
    order_payload: dict[str, Any]
    broker_response: dict[str, Any] | None
    created_at: str
    strategy_id: uuid.UUID | None = None
    outcome: str = "ok"
    failure_reason: str | None = None


# --- Live Trading (Build Spec §12) ------------------------------------------


class EnrollLiveTradingRequest(BaseModel):
    strategy_id: uuid.UUID
    symbol: str = Field(min_length=1, max_length=32)
    broker_name: str = Field(min_length=1, max_length=32)
    builtin_strategy: EngineBuiltinStrategy = "sma_crossover"
    sma_window: int = Field(default=15, gt=0)
    initial_capital: float = Field(default=100_000.0, gt=0)
    stop_loss_pct: float = Field(default=3.0, gt=0)
    position_size_pct: float = Field(default=5.0, gt=0, le=100)
    intent_expiry_seconds: int = Field(default=90, gt=0, le=300)
    # Phase 18 Part 3's standing caps -- conservative defaults, editable
    # here at enrollment (or later via a direct update); never optional
    # once autonomy is enabled. Deliberately no `autonomous_trading_enabled`
    # field here -- see EnrollLiveTradingRequest's sibling
    # SetAutonomousTradingRequest below, the only way to actually enable it.
    max_intents_per_window: int = Field(default=5, gt=0)
    rate_limit_window_minutes: int = Field(default=60, gt=0)
    max_notional_per_intent: float = Field(default=50_000.0, gt=0)


class LiveTradingSubscriptionResponse(BaseModel):
    id: uuid.UUID
    strategy_id: uuid.UUID
    symbol: str
    broker_name: str
    builtin_strategy: str
    sma_window: int
    initial_capital: float
    stop_loss_pct: float
    position_size_pct: float
    intent_expiry_seconds: int
    is_active: bool
    autonomous_trading_enabled: bool
    autonomy_enabled_by: str | None
    autonomy_enabled_at: str | None
    max_intents_per_window: int
    rate_limit_window_minutes: int
    max_notional_per_intent: float


class SetAutonomousTradingRequest(BaseModel):
    """The master switch's only HTTP entry point (Non-Negotiable Rule
    #1) -- deliberately just one field, no partial-update ambiguity."""

    enabled: bool


class GenerateLiveOrderIntentRequest(BaseModel):
    tick_price: float = Field(gt=0)


class LiveOrderIntentResponse(BaseModel):
    id: uuid.UUID
    strategy_id: uuid.UUID
    symbol: str
    side: str
    quantity: int
    intent_type: str
    generated_at: str
    expires_at: str
    status: str
    approved_by: str | None
    approved_at: str | None
    resulting_order_id: uuid.UUID | None
    batch_authorization_id: uuid.UUID | None


class LivePositionResponse(BaseModel):
    strategy_id: uuid.UUID
    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float


class OrderResponse(BaseModel):
    id: uuid.UUID
    live_order_intent_id: uuid.UUID
    strategy_id: uuid.UUID
    symbol: str
    side: str
    quantity: int
    broker_name: str
    broker_order_id: str | None
    status: str
    failure_reason: str | None
    submitted_at: str


class TradeResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    symbol: str
    side: str
    quantity: int
    price: float
    status: str
    data_source: str = "unknown"
    executed_at: str


# --- Phase 10: Market Data & Data Lake (Build Spec §14) ---------------------


class MarketPulseResponse(BaseModel):
    as_of: str
    india_vix: float
    sector_indices_change_pct: dict[str, float]
    global_indices_change_pct: dict[str, float]


class MarketHoursResponse(BaseModel):
    is_open: bool
    as_of: str


class HolidayEntryResponse(BaseModel):
    date: str
    weekday: str
    name: str | None
    source: str
    on_weekend: bool


class HolidaysResponse(BaseModel):
    year: int
    holidays: list[HolidayEntryResponse]
    # False: the calendar file lists nothing for this year, so its movable holidays (Holi, Eid,
    # Diwali ...) are being treated as trading days. `warning` says so in words.
    calendar_file_covers_year: bool
    warning: str | None


class IndicatorSeriesResponse(BaseModel):
    symbol: str
    dates: list[str]
    close: list[float]
    sma: list[float | None]
    sma_window: int
    ema: list[float | None]
    ema_span: int
    rsi: list[float | None]
    rsi_period: int
    macd: list[float | None]
    macd_signal: list[float | None]
    macd_histogram: list[float | None]
    bollinger_upper: list[float | None]
    bollinger_middle: list[float | None]
    bollinger_lower: list[float | None]


class FreshnessRecordResponse(BaseModel):
    symbol: str
    data_type: str
    data_date: date_type
    row_count: int
    ingested_at: str


class LiveOptionChainEntryResponse(BaseModel):
    """Mirrors src.brokers.base.OptionChainEntry -- a real per-strike row
    from whichever broker is configured (Phase 16 audit follow-up D).
    Every optional field is genuinely `None`, never a fabricated `0.0`,
    when the broker's own response didn't carry it.

    `call_iv`/`put_iv` are only ever a real, broker-reported figure
    (Upstox). `call_iv_computed`/`put_iv_computed` are a *modeled*
    Black-Scholes estimate (`src.engine.options_pricing`), filled in by
    the route only when the broker itself reported no IV (Zerodha,
    whose API has no options-greeks field at all) and enough real data
    (LTP, spot, time to expiry) exists to solve for one -- the two are
    never merged into a single field, so a client can never mistake a
    modeled number for a broker-reported one."""

    strike: float
    call_symbol: str | None
    put_symbol: str | None
    call_ltp: float | None
    put_ltp: float | None
    call_oi: float | None
    put_oi: float | None
    call_iv: float | None
    put_iv: float | None
    call_iv_computed: float | None = None
    put_iv_computed: float | None = None


class LiveOptionChainResponse(BaseModel):
    """`underlying_ltp`/`atm_strike` are best-effort: a failed spot-quote
    lookup (e.g. the underlying's own instrument key doesn't resolve to a
    quotable symbol) leaves both `None` rather than failing the whole
    chain response, which already has real per-strike data worth
    returning on its own."""

    broker: str
    underlying: str
    expiry: str
    underlying_ltp: float | None = None
    atm_strike: float | None = None
    entries: list[LiveOptionChainEntryResponse]


class InstrumentResponse(BaseModel):
    symbol: str
    exchange: str
    instrument_type: str
    isin: str | None
    lot_size: int
    tick_size: float
    underlying_symbol: str | None
    expiry_date: date_type | None
    strike_price: float | None
    option_type: str | None
    is_active: bool
    last_synced_at: str


class MarketDataProvenanceResponse(BaseModel):
    id: uuid.UUID
    pipeline: str
    source: str
    status: str
    symbols_processed: int
    rows_ingested: int
    error_message: str | None
    details: dict[str, Any] | None
    started_at: str
    completed_at: str


class PipelineStatusEntry(BaseModel):
    pipeline: str
    last_run_status: str | None
    last_run_at: str | None
    last_run_source: str | None
    last_error: str | None
    last_success_at: str | None


class DatalakeStatusResponse(BaseModel):
    as_of: str
    pipelines: list[PipelineStatusEntry]
    symbols_with_daily_data: int
    symbols_with_intraday_data: int
    most_recent_daily_data_date: date_type | None
    most_recent_intraday_data_date: date_type | None


class RunDailyIngestionRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    as_of: date_type | None = None


class RunIntradayIngestionRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    day: date_type | None = None


class RunCorporateActionsIngestionRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    since: date_type | None = None


class RunInstrumentMasterSyncRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)


class RunBhavcopyFallbackRequest(BaseModel):
    symbols: list[str] = Field(min_length=1)
    day: date_type
    segment: Literal["equity", "fo"] = "equity"


# --- Phase 11: Audit & Observability (Build Spec §19) -----------------------


class AuditLogEntryResponse(BaseModel):
    id: uuid.UUID
    sequence: int
    previous_hash: str
    hash: str
    actor: str
    action: str
    entity_type: str | None
    entity_id: str | None
    details: dict[str, Any] | None
    correlation_id: str | None
    created_at: str


class AuditChainVerifyResponse(BaseModel):
    diverged: bool
    archive_internally_valid: bool
    live_db_matches_archive: bool
    first_diverged_sequence: int | None
    reason: str | None
    entries_checked_in_db_chain: int
    db_chain_valid: bool
    db_chain_reason: str | None


class AuditTradeTraceResponse(BaseModel):
    """Phase 21 (docs/phase20-old-vs-new-comparison.md item 26): every real
    audit_log row for one entity_id, in chronological order -- the same
    data GET /audit/entries?entity_id=... already returns, shaped as a
    purpose-built trace instead of a generic filtered list."""

    entity_id: str
    entry_count: int
    entries: list[AuditLogEntryResponse]


class AuditActorSummaryResponse(BaseModel):
    actor: str
    action_count: int
    first_seen: str | None
    last_seen: str | None
    # action -> count, most-frequent first.
    actions: dict[str, int]


# --- Phase 12: Notifications & Omni-Channel (Build Spec §18) ----------------


def _check_alert_levels(levels: list[str] | None) -> list[str] | None:
    """Only the fixed alert levels can be subscribed to: an unknown string used to be stored and
    never matched, so the channel silently received nothing."""
    from src.notifications.types import AlertLevel

    if levels is None:
        return levels
    known = {level.value for level in AlertLevel}
    unknown = sorted(set(levels) - known)
    if unknown:
        raise ValueError(f"unknown alert level(s) {unknown}; known: {sorted(known)}")
    return levels


class WriteNotificationChannelRequest(BaseModel):
    """Write-only by design (Build Spec §20), same posture as
    WriteBrokerCredentialsRequest above -- no response schema echoes
    bot_token/webhook_url/webhook_secret_token/public_key/signing_secret
    back. allowed_sender_ids/alert_levels/enabled are not secrets and do
    come back on NotificationChannelStatusResponse.

    A field that is **absent** from the request keeps the channel's stored value; a field sent
    as `null` (or `[]`) clears it. Before, an absent field was cleared, so saving only the alert
    levels erased the channel's token and chat id."""

    enabled: bool = True
    bot_token: str | None = None
    chat_id: str | None = None
    webhook_url: str | None = None
    webhook_secret_token: str | None = None
    public_key: str | None = None
    signing_secret: str | None = None
    allowed_sender_ids: list[str] = Field(default_factory=list)
    alert_levels: list[str] = Field(default_factory=list)

    @field_validator("alert_levels")
    @classmethod
    def _known_alert_levels(cls, levels: list[str]) -> list[str]:
        return _check_alert_levels(levels) or []


class UpdateNotificationChannelRequest(BaseModel):
    """Change a configured channel's non-secret settings. It cannot carry a secret, so a client
    that only toggles an alert level has no way to touch the token. At least one field is needed;
    `alert_levels: []` unsubscribes the channel from everything."""

    enabled: bool | None = None
    alert_levels: list[str] | None = None
    allowed_sender_ids: list[str] | None = None

    @field_validator("alert_levels")
    @classmethod
    def _known_alert_levels(cls, levels: list[str] | None) -> list[str] | None:
        return _check_alert_levels(levels)


class NotificationChannelStatusResponse(BaseModel):
    channel: str
    configured: bool
    enabled: bool
    allowed_sender_ids: list[str]
    alert_levels: list[str]


class EngineAlertResponse(BaseModel):
    id: uuid.UUID
    key: str
    source: Literal["watcher", "event", "alertmanager"]
    kind: str
    severity: Literal["critical", "warning", "info"]
    title: str
    detail: str
    subscription_id: uuid.UUID | None
    opened_at: datetime_type
    last_seen_at: datetime_type
    # None while it is still happening.
    resolved_at: datetime_type | None
    # When the opening notice went out to the channels; None if it cleared before it had lasted
    # long enough to be announced.
    notified_at: datetime_type | None


class EngineAlertListResponse(BaseModel):
    # Across all sources and independent of `limit`, so the top-bar count is the real number.
    open_count: int
    critical_open_count: int
    alerts: list[EngineAlertResponse]


class AlertmanagerStatusResponse(BaseModel):
    # False when the deployment has no Alertmanager (ALERTMANAGER_URL unset).
    configured: bool
    # None when it was not asked (not configured); otherwise whether /-/healthy answered.
    reachable: bool | None
    detail: str
    # The webhook token exists on its volume.
    token_ready: bool
    # Newest alert Prometheus has sent through it: the proof the whole path works.
    last_alert_received_at: datetime_type | None


class DetectTelegramChatIdRequest(BaseModel):
    bot_token: str = Field(min_length=1)


class DetectTelegramChatIdResponse(BaseModel):
    ok: bool
    chat_id: str | None = None
    chat_label: str | None = None
    detail: str


class TestNotificationChannelRequest(BaseModel):
    """All fields optional and override-only: an empty body tests whatever
    is already saved for this channel; any field set here is used in place
    of the saved value for this one test call without persisting it --
    "test before you save" for a wizard step, per WriteNotificationChannelRequest's
    same fields."""

    bot_token: str | None = None
    chat_id: str | None = None
    webhook_url: str | None = None


class TestNotificationChannelResponse(BaseModel):
    channel: str
    ok: bool
    status_code: int | None = None
    error: str | None = None
    tested_at: str


# --- Settings redesign: LLM provider credentials + test/discovery ----------


class WriteLlmProviderCredentialsRequest(BaseModel):
    """Write-only by design, same posture as WriteBrokerCredentialsRequest
    above -- api_key is never echoed back. base_url is required for
    "custom" (no api_key needed there); optional for every hosted provider
    (only meaningful as an override, e.g. an OpenAI-compatible proxy)."""

    api_key: str | None = None
    base_url: str | None = None
    # What `model: "auto"` resolves to on this provider. Omitted keeps the
    # stored one, so re-entering a key doesn't lose the chosen model.
    default_model: str | None = None


class WriteLlmProviderDefaultModelRequest(BaseModel):
    """`null` clears it (falls back to the built-in default, if any)."""

    model: str | None = Field(default=None, max_length=200)


class LlmProviderStatusResponse(BaseModel):
    provider: str
    configured: bool
    base_url: str | None = None
    in_fallback_order: bool
    # Operator-chosen model for `auto`, and the built-in one used when
    # none is chosen (None for Ollama/Custom/Hugging Face, which need one).
    default_model: str | None = None
    builtin_default_model: str | None = None


class LlmProviderTestResult(BaseModel):
    provider: str
    ok: bool
    detail: str
    latency_ms: float | None = None


class LlmProviderModel(BaseModel):
    id: str
    label: str | None = None


class LlmProviderModelsResponse(BaseModel):
    provider: str
    models: list[LlmProviderModel]


# --- Phase 12: In-app chat (Build Spec §18) ---------------------------------


class CreateChatSessionRequest(BaseModel):
    title: str = Field(default="New chat", max_length=200)
    model: str | None = None


class UpdateChatSessionRequest(BaseModel):
    title: str | None = None
    model: str | None = None
    clear_model: bool = False
    pinned: bool | None = None


class ChatSessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    model: str | None
    pinned: bool
    created_by: str
    created_at: datetime_type
    updated_at: datetime_type


class ChatMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    provider: str | None
    aborted: bool
    created_at: datetime_type


class SendChatMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class HostVitals(BaseModel):
    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    uptime_seconds: float


class LlmProviderHealthVitals(BaseModel):
    """One provider's real failure/success tracking from the live
    `LlmRouter` singleton (src.agents.llm_router.ProviderHealth) --
    `last_failure_at`/`last_success_at` are `None` until that provider has
    genuinely been called at least once this process's lifetime, never a
    fabricated timestamp."""

    provider: str
    last_failure_at: str | None
    last_success_at: str | None
    served_as_fallback: bool


class LlmVitals(BaseModel):
    """`token_usage_today` only ever contains a provider once it has
    genuinely been called today (Asia/Kolkata calendar day) -- a provider
    absent from the dict is honestly "no calls yet today", never a
    fabricated `0`. `provider_health` is in the router's live fallback
    order (first entry is the currently-active provider)."""

    active_provider: str | None
    token_usage_today: dict[str, int]
    provider_health: list[LlmProviderHealthVitals]


class LatencyVitals(BaseModel):
    """A real trailing-`window` percentile queried from Prometheus at
    request time (src.observability.vitals), not a since-start average --
    `None` when Prometheus is unreachable or the window has no samples yet,
    never a fabricated `0.0`."""

    order_dispatch_p50_ms: float | None
    order_dispatch_p95_ms: float | None
    window: str
    budget_ms: float


class MarketVitals(BaseModel):
    is_open: bool
    next_event_at: str


class SystemVitalsResponse(BaseModel):
    """See src.observability.vitals's module docstring for why each of
    these three groups is read a genuinely different way rather than all
    forced through one shared "since process start" shape."""

    host: HostVitals
    llm: LlmVitals
    latency: LatencyVitals
    market: MarketVitals
    as_of: str
