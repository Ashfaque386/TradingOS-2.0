import uuid
from datetime import date as date_type
from datetime import datetime as datetime_type
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

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
    code: str
    static_validation_passed: bool
    static_validation_errors: list[str] | None
    sandbox_passed: bool | None
    sandbox_result: dict | None
    options_legs: dict | None
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
    reason: str | None
    decided_by: str | None
    decided_at: datetime_type | None
    created_at: datetime_type


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
    metrics: dict | None
    daily_returns: list | None
    trade_pnls: list[float] | None
    walk_forward_result: dict | None
    monte_carlo_result: dict | None
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
    reason: str | None
    status: str
    staged_by: str
    staged_at: datetime_type
    confirmed_by: str | None
    confirmed_at: datetime_type | None
    applied_by: str | None
    applied_at: datetime_type | None


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


# --- Paper Trading Engine (Build Spec §11) ----------------------------------


class EnrollPaperTradingRequest(BaseModel):
    strategy_version_id: uuid.UUID
    symbol: str = Field(min_length=1, max_length=32)
    builtin_strategy: EngineBuiltinStrategy = "sma_crossover"
    sma_window: int = Field(default=15, gt=0)
    initial_capital: float = Field(default=100_000.0, gt=0)
    stop_loss_pct: float = Field(default=3.0, gt=0)
    position_size_pct: float = Field(default=5.0, gt=0, le=100)


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


class PaperPositionResponse(BaseModel):
    subscription_id: uuid.UUID
    symbol: str
    quantity: int
    avg_cost: float
    realized_pnl: float


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
    created_at: str


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


class BrokerCredentialStatusResponse(BaseModel):
    broker: str
    configured: bool


class ShadowModeCheckRequest(BaseModel):
    broker: str = Field(min_length=1)
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
    order_payload: dict
    broker_response: dict | None
    created_at: str


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


class CreateBatchAuthorizationRequest(BaseModel):
    strategy_id: uuid.UUID
    max_intents: int = Field(gt=0)
    max_notional_per_intent: float = Field(gt=0)
    window_start: datetime_type
    window_end: datetime_type


class LiveBatchAuthorizationResponse(BaseModel):
    id: uuid.UUID
    strategy_id: uuid.UUID
    authorized_by: str
    max_intents: int
    max_notional_per_intent: float
    intents_used: int
    window_start: str
    window_end: str


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


class FreshnessRecordResponse(BaseModel):
    symbol: str
    data_type: str
    data_date: date_type
    row_count: int
    ingested_at: str


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
    details: dict | None
    started_at: str
    completed_at: str


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
    details: dict | None
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


# --- Phase 12: Notifications & Omni-Channel (Build Spec §18) ----------------


class WriteNotificationChannelRequest(BaseModel):
    """Write-only by design (Build Spec §20), same posture as
    WriteBrokerCredentialsRequest above -- no response schema echoes
    bot_token/webhook_url/webhook_secret_token/public_key/signing_secret
    back. allowed_sender_ids/alert_levels/enabled are not secrets and do
    come back on NotificationChannelStatusResponse."""

    enabled: bool = True
    bot_token: str | None = None
    chat_id: str | None = None
    webhook_url: str | None = None
    webhook_secret_token: str | None = None
    public_key: str | None = None
    signing_secret: str | None = None
    allowed_sender_ids: list[str] = Field(default_factory=list)
    alert_levels: list[str] = Field(default_factory=list)


class NotificationChannelStatusResponse(BaseModel):
    channel: str
    configured: bool
    enabled: bool
    allowed_sender_ids: list[str]
    alert_levels: list[str]


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
