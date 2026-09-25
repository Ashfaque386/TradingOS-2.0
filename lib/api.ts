// Thin fetch wrapper for the real TradingOS backend (Phase 13 frontend
// wiring). No swr/react-query/axios is installed, so this is deliberately
// plain `fetch` plus JWT storage + a single-flight refresh-on-401 retry --
// every page/component should go through here rather than calling
// `fetch` directly, so auth and error handling stay in one place.

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

type TokenPair = { accessToken: string; refreshToken: string }

const TOKENS_KEY = 'tradingos.tokens'

function loadTokens(): TokenPair | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(TOKENS_KEY)
    return raw ? (JSON.parse(raw) as TokenPair) : null
  } catch {
    return null
  }
}

function saveTokens(tokens: TokenPair) {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(TOKENS_KEY, JSON.stringify(tokens))
  } catch {
    // localStorage unavailable (private mode, blocked storage) -- the
    // session just won't survive a refresh, which is the same failure
    // mode as not persisting tokens at all.
  }
}

function clearTokens() {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.removeItem(TOKENS_KEY)
  } catch {
    // ignore
  }
}

export function getAccessToken(): string | null {
  return loadTokens()?.accessToken ?? null
}

export function isAuthenticated(): boolean {
  return getAccessToken() !== null
}

// Concurrent 401s must not each fire their own refresh (that would race
// the backend's single-use refresh-token rotation and revoke the whole
// family) -- every caller in flight shares the one in-progress refresh.
let refreshPromise: Promise<TokenPair | null> | null = null

async function refreshTokens(): Promise<TokenPair | null> {
  const current = loadTokens()
  if (!current) return null
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: current.refreshToken }),
        })
        if (!res.ok) {
          clearTokens()
          return null
        }
        const data = (await res.json()) as { access_token: string; refresh_token: string }
        const next: TokenPair = { accessToken: data.access_token, refreshToken: data.refresh_token }
        saveTokens(next)
        return next
      } catch {
        return null
      } finally {
        refreshPromise = null
      }
    })()
  }
  return refreshPromise
}

async function request<T>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
  const token = getAccessToken()
  const headers = new Headers(init.headers)
  if (!headers.has('Content-Type') && init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers })

  if (res.status === 401 && allowRefresh && token) {
    const refreshed = await refreshTokens()
    if (refreshed) return request<T>(path, init, false)
  }

  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      // no JSON body
    }
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }

  if (res.status === 204) return undefined as T
  const text = await res.text()
  return (text ? JSON.parse(text) : undefined) as T
}

export const apiGet = <T>(path: string) => request<T>(path, { method: 'GET' })
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined })
export const apiPut = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined })
export const apiPatch = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined })
export const apiDelete = <T>(path: string) => request<T>(path, { method: 'DELETE' })

// ---- Auth ----
// Role values match src/core/roles.py's Role StrEnum exactly.
export type Role = 'SystemAdministrator' | 'PortfolioManager' | 'RiskManager' | 'ReadOnlyAuditor'

export type CurrentUser = {
  id: string
  email: string
  role: Role
  isActive: boolean
}

function toCurrentUser(data: { id: string; email: string; role: Role; is_active: boolean }): CurrentUser {
  return { id: data.id, email: data.email, role: data.role, isActive: data.is_active }
}

export async function login(email: string, password: string): Promise<CurrentUser> {
  const data = await request<{ access_token: string; refresh_token: string }>(
    '/api/v1/auth/login',
    { method: 'POST', body: JSON.stringify({ email, password }) },
    false,
  )
  saveTokens({ accessToken: data.access_token, refreshToken: data.refresh_token })
  return getMe()
}

// The very first user ever registered on a fresh system automatically
// becomes SystemAdministrator (src/api/routes/auth.py); every user after
// that is capped at ReadOnlyAuditor, since granting higher roles is an
// admin action, not self-service. /auth/register returns the created
// user only (no tokens), so this logs in right after to hand back an
// authenticated session in one step -- the password never touches disk,
// here or on the backend: it's hashed immediately and only the hash is
// persisted.
export async function register(email: string, password: string): Promise<CurrentUser> {
  await request<{ id: string; email: string; role: Role; is_active: boolean }>(
    '/api/v1/auth/register',
    { method: 'POST', body: JSON.stringify({ email, password }) },
    false,
  )
  return login(email, password)
}

export async function getMe(): Promise<CurrentUser> {
  const data = await apiGet<{ id: string; email: string; role: Role; is_active: boolean }>(
    '/api/v1/auth/me',
  )
  return toCurrentUser(data)
}

export async function logout(): Promise<void> {
  const tokens = loadTokens()
  clearTokens()
  if (!tokens) return
  try {
    await fetch(`${API_BASE_URL}/api/v1/auth/logout`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: tokens.refreshToken }),
    })
  } catch {
    // best-effort revoke -- tokens are already cleared client-side either way
  }
}

// ---- Agents (src/gateway/roster.py's fixed 24-agent roster) ----
export type AgentSummary = {
  agent_id: string
  display_name: string
  department: string
  can_disable: boolean
  enabled: boolean
  capabilities: string[]
  skills: string[]
  heartbeat_enabled: boolean
  identity_name: string
  emoji: string | null
  avatar: string | null
  theme: string | null
  voice: string | null
  // Phase 19 (docs/phase19-audit.md §3.2): false only for the 3 agents
  // whose subject matter (fundamentals, valuation, macro/economic
  // calendar) has no real backing data source anywhere in this codebase.
  has_data_source: boolean
}

export const getAgents = () => apiGet<AgentSummary[]>('/api/v1/agents')

// ---- Per-agent activity (Phase 19 Part 3.2) -- real HeartbeatLog rows
// plus orchestration Tasks joined by capability, never a fabricated
// per-agent metric. ----
export type AgentHeartbeatEntry = {
  status: string
  details: Record<string, unknown> | null
  checked_at: string
}
export type AgentTaskEntry = {
  id: string
  run_id: string
  name: string
  status: string
  last_error: string | null
  created_at: string
  completed_at: string | null
}
export type AgentActivity = {
  agent_id: string
  heartbeats: AgentHeartbeatEntry[]
  tasks: AgentTaskEntry[]
}
export const getAgentActivity = (agentId: string) =>
  apiGet<AgentActivity>(`/api/v1/agents/${agentId}/activity`)

export type SetAgentIdentityInput = {
  name?: string
  emoji?: string
  avatar?: string
  theme?: string
  voice?: string
}

export const putAgentIdentity = (agentId: string, body: SetAgentIdentityInput) =>
  apiPut<ApplyResult>(`/api/v1/agents/${agentId}/identity`, body)

export type PromptVersion = {
  id: string
  agent_id: string
  version_number: number
  content: string
  status: 'draft' | 'active' | 'superseded'
  diff_from_previous: string | null
  created_by: string | null
  created_at: string
  activated_at: string | null
}

export const listPromptVersions = (agentId: string) =>
  apiGet<PromptVersion[]>(`/api/v1/agents/${agentId}/prompt-versions`)
export const createPromptVersion = (agentId: string, content: string) =>
  apiPost<PromptVersion>(`/api/v1/agents/${agentId}/prompt-versions`, { content })
export const activatePromptVersion = (agentId: string, versionId: string) =>
  apiPost<PromptVersion>(`/api/v1/agents/${agentId}/prompt-versions/${versionId}/activate`)

// ---- Kill switch ----
export type KillSwitchMode = 'live' | 'paper'
export type KillSwitchState = {
  mode: string
  tripped: boolean
  trip_reason: string | null
  last_drawdown_pct: number | null
  threshold_pct: number
}

export const getKillSwitch = (mode: KillSwitchMode) =>
  apiGet<KillSwitchState>(`/api/v1/kill-switch/${mode}`)
export const resetKillSwitch = (mode: KillSwitchMode, reason?: string) =>
  apiPost<KillSwitchState>(`/api/v1/kill-switch/${mode}/reset`, { reason })

export type TodaysPaperPnl = {
  as_of_date: string
  realized_pnl: number
  fill_count: number
}
export const getTodaysPaperPnl = () => apiGet<TodaysPaperPnl>('/api/v1/paper-trading/pnl/today')

export type UnrealizedPaperPnl = {
  as_of: string
  price_source: 'real' | 'synthetic'
  unrealized_pnl: number | null
  positions_priced: number
  positions_unpriced: number
}
export const getUnrealizedPaperPnl = () =>
  apiGet<UnrealizedPaperPnl>('/api/v1/paper-trading/pnl/unrealized')

// ---- Orchestration runs ----
export type TaskSummary = {
  id: string
  plan_key: string
  capability: string
  name: string
  status: string
  failure_class: string | null
  last_error: string | null
}

export type OrganizationRun = {
  id: string
  objective: string
  source: string
  run_type: string
  status: string
  failure_class: string | null
  source_run_id: string | null
  error: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  tasks: TaskSummary[]
}

export const listRuns = () => apiGet<OrganizationRun[]>('/api/v1/orchestration/runs')
export const getRun = (runId: string) => apiGet<OrganizationRun>(`/api/v1/orchestration/runs/${runId}`)
export const createRun = (objective: string) =>
  apiPost<OrganizationRun>('/api/v1/orchestration/runs', { objective })
export const pauseRun = (runId: string) =>
  apiPost<OrganizationRun>(`/api/v1/orchestration/runs/${runId}/pause`)
export const continueRun = (runId: string) =>
  apiPost<OrganizationRun>(`/api/v1/orchestration/runs/${runId}/continue`)
export const retryRun = (runId: string) =>
  apiPost<OrganizationRun>(`/api/v1/orchestration/runs/${runId}/retry`)
export const rerunRun = (runId: string) =>
  apiPost<OrganizationRun>(`/api/v1/orchestration/runs/${runId}/rerun`)

// ---- Approvals (strategy promotion / live-eligibility sign-off) ----
export type ApprovalRequestDto = {
  id: string
  subject_type: string
  subject_id: string
  transition_type: string
  status: string
  requested_by: string | null
  reason: string | null
  decided_by: string | null
  decided_at: string | null
  created_at: string
}

export const listApprovals = (statusFilter?: string) =>
  apiGet<ApprovalRequestDto[]>(
    `/api/v1/approvals${statusFilter ? `?status_filter=${statusFilter}` : ''}`,
  )
export const decideApproval = (approvalId: string, approve: boolean, reason?: string) =>
  apiPost<ApprovalRequestDto>(`/api/v1/approvals/${approvalId}/decide`, { approve, reason })

// ---- Live trading (Phase 18: fully autonomous once explicitly enabled --
// no per-order approve/reject, see backend Non-Negotiable Rule #1) ----
export type LiveOrderIntentDto = {
  id: string
  strategy_id: string
  symbol: string
  side: string
  quantity: number
  intent_type: string
  generated_at: string
  expires_at: string
  status: string
  approved_by: string | null
  approved_at: string | null
  resulting_order_id: string | null
  batch_authorization_id: string | null
}

export const listLiveIntents = (statusFilter?: string) =>
  apiGet<LiveOrderIntentDto[]>(
    `/api/v1/live-trading/intents${statusFilter ? `?status_filter=${statusFilter}` : ''}`,
  )

export type LiveTradingSubscriptionDto = {
  id: string
  strategy_id: string
  symbol: string
  broker_name: string
  builtin_strategy: string
  sma_window: number
  initial_capital: number
  stop_loss_pct: number
  position_size_pct: number
  intent_expiry_seconds: number
  is_active: boolean
  autonomous_trading_enabled: boolean
  autonomy_enabled_by: string | null
  autonomy_enabled_at: string | null
  max_intents_per_window: number
  rate_limit_window_minutes: number
  max_notional_per_intent: number
}

export type EnrollLiveTradingInput = {
  strategy_id: string
  symbol: string
  broker_name: string
  builtin_strategy?: string
  sma_window?: number
  initial_capital?: number
  stop_loss_pct?: number
  position_size_pct?: number
  intent_expiry_seconds?: number
  max_intents_per_window?: number
  rate_limit_window_minutes?: number
  max_notional_per_intent?: number
}

export const listLiveSubscriptions = () =>
  apiGet<LiveTradingSubscriptionDto[]>('/api/v1/live-trading/subscriptions')
export const enrollLiveTrading = (body: EnrollLiveTradingInput) =>
  apiPost<LiveTradingSubscriptionDto>('/api/v1/live-trading/subscriptions', body)
export const setLiveAutonomy = (subscriptionId: string, enabled: boolean) =>
  apiPost<LiveTradingSubscriptionDto>(
    `/api/v1/live-trading/subscriptions/${subscriptionId}/autonomy`,
    { enabled },
  )
// Manual intent-generation trigger -- exercises the exact same
// generate_live_order_intent function the scheduler calls automatically;
// useful for ops visibility and demonstration, never a required step.
export const generateLiveIntent = (subscriptionId: string, tickPrice: number) =>
  apiPost<LiveOrderIntentDto | null>(
    `/api/v1/live-trading/subscriptions/${subscriptionId}/generate-intent`,
    { tick_price: tickPrice },
  )

// ---- WebSocket payload shapes (src/api/routes/websockets.py) ----
export type ActivityEvent = {
  id: string
  sequence: number
  actor: string
  action: string
  entity_type: string
  entity_id: string
  details: unknown
  created_at: string
}
export type ActivityFeedMessage =
  | { type: 'backfill'; events: ActivityEvent[] }
  | { type: 'event'; event: ActivityEvent }

export type SignoffSnapshot = {
  type: 'snapshot'
  server_time: string
  approvals: ApprovalRequestDto[]
  intents: LiveOrderIntentDto[]
}

export type OrganizationEventMessage = { type: 'event'; event: Record<string, unknown> }

// ---- Automation schedule (Phase 19, docs/phase19-audit.md Part 1.2) --
// real live next_run_time read off the running APScheduler instances,
// never a hardcoded cadence string. Heartbeat is a plain asyncio loop
// (src.agents.scheduler.start_heartbeat_loop), not an APScheduler job, so
// it's reported separately as a fixed interval instead of a fabricated
// next_run_time. ----
export type ScheduledJob = {
  scheduler: string
  job_id: string
  trigger: string
  next_run_time: string | null
}
export type ScheduledJobsResponse = {
  jobs: ScheduledJob[]
  heartbeat_interval_seconds: number
}
export const getScheduledJobs = () => apiGet<ScheduledJobsResponse>('/api/v1/system/scheduled-jobs')

// ---- Scheduled job run-now / edit-schedule / run-history (Phase 22,
// docs/phase20-old-vs-new-comparison.md item 20) -- mutating the live
// APScheduler job in place, plus a durable history of real past firings
// (src.observability.scheduled_job_history) since APScheduler itself
// forgets a firing once it completes. ----
export type RunScheduledJobNowResponse = {
  scheduler: string
  job_id: string
  next_run_time: string | null
}
export const runScheduledJobNow = (scheduler: string, jobId: string) =>
  apiPost<RunScheduledJobNowResponse>(
    `/api/v1/system/scheduled-jobs/${encodeURIComponent(scheduler)}/${encodeURIComponent(jobId)}/run-now`
  )

export type UpdateScheduledJobRequest = {
  seconds?: number
  hour?: number
  minute?: number
}
export type UpdateScheduledJobResponse = {
  scheduler: string
  job_id: string
  trigger: string
  next_run_time: string | null
}
export const updateScheduledJobSchedule = (
  scheduler: string,
  jobId: string,
  payload: UpdateScheduledJobRequest
) =>
  apiPut<UpdateScheduledJobResponse>(
    `/api/v1/system/scheduled-jobs/${encodeURIComponent(scheduler)}/${encodeURIComponent(jobId)}/schedule`,
    payload
  )

export type ScheduledJobRunHistoryEntry = {
  scheduled_run_time: string
  finished_at: string
  status: 'succeeded' | 'failed'
  error: string | null
}
export type ScheduledJobHistoryResponse = {
  runs: ScheduledJobRunHistoryEntry[]
}
export const getScheduledJobHistory = (scheduler: string, jobId: string) =>
  apiGet<ScheduledJobHistoryResponse>(
    `/api/v1/system/scheduled-jobs/${encodeURIComponent(scheduler)}/${encodeURIComponent(jobId)}/history`
  )

// ---- Live Canvas (Phase 22, docs/phase20-old-vs-new-comparison.md item 19) ----
// One composed read over real, already-persisted data -- the newest
// strategy code, newest completed backtest, newest audit event. ----
export type CanvasLatestStrategyCode = {
  strategy_id: string
  strategy_name: string
  version_id: string
  version_number: number
  code: string
  created_at: string
}
export type CanvasLatestBacktestResult = {
  backtest_id: string
  strategy_id: string
  strategy_name: string
  symbol: string
  status: string
  metrics: Record<string, number> | null
  created_at: string
}
export type CanvasLatestAgentLog = {
  sequence: number
  actor: string
  action: string
  entity_type: string | null
  entity_id: string | null
  created_at: string
}
export type CanvasState = {
  latest_strategy_code: CanvasLatestStrategyCode | null
  latest_backtest_result: CanvasLatestBacktestResult | null
  latest_agent_log: CanvasLatestAgentLog | null
}
export const getCanvasState = () => apiGet<CanvasState>('/api/v1/canvas/state')

// ---- Agent run analytics (Phase 22, item 21) ----
export type AgentAnalyticsSummaryRow = {
  agent_id: string
  display_name: string
  tasks_total: number
  tasks_succeeded: number
  tasks_failed: number
  success_rate: number | null
  avg_duration_seconds: number | null
}
export type AgentAnalyticsTrendPoint = {
  date: string
  tasks_succeeded: number
  tasks_failed: number
}
export const getAgentAnalyticsSummary = () =>
  apiGet<AgentAnalyticsSummaryRow[]>('/api/v1/agents/analytics/summary')
export const getAgentAnalyticsTrend = (days = 14) =>
  apiGet<AgentAnalyticsTrendPoint[]>(`/api/v1/agents/analytics/trend?days=${days}`)

// ---- Operator Guidance (Phase 19 Part 2.6/3.1) -- notes the CEO Agent's
// next planning cycle actually reads, see run_control.py's
// fold_guidance_into_objective(). ----
export type OperatorGuidanceDto = {
  id: string
  message: string
  created_by: string
  is_active: boolean
  created_at: string
  deactivated_at: string | null
}
export const listOperatorGuidance = () => apiGet<OperatorGuidanceDto[]>('/api/v1/operator-guidance')
export const createOperatorGuidance = (message: string) =>
  apiPost<OperatorGuidanceDto>('/api/v1/operator-guidance', { message })
export const deactivateOperatorGuidance = (guidanceId: string) =>
  apiPost<OperatorGuidanceDto>(`/api/v1/operator-guidance/${guidanceId}/deactivate`)

// ---- Strategies ----
export type StrategyVersion = {
  id: string
  version_number: number
  code: string
  static_validation_passed: boolean
  static_validation_errors: string[] | null
  sandbox_passed: boolean | null
  sandbox_result: Record<string, unknown> | null
  options_legs: Record<string, unknown> | null
  created_at: string
}

export type Strategy = {
  id: string
  name: string
  objective: string
  instrument_class: string
  status: string
  versions: StrategyVersion[]
}

export type SuggestionDto = {
  id: string
  strategy_id: string
  base_version_id: string
  suggestion_text: string
  status: string
  ai_verdict: Record<string, unknown> | null
  regenerated_version_id: string | null
  regeneration_diff: string | null
}

export type GoLiveReadinessInput = {
  num_trades: number
  calendar_days_running: number
  clean_shadow_mode_streak_days: number
  live_win_rate?: number | null
  backtest_win_rate?: number | null
}

export type GoLiveReadinessResult = {
  eligible: boolean
  checks: Record<string, boolean>
  reasons: string[]
}

export const listStrategies = () => apiGet<Strategy[]>('/api/v1/strategies')
export const getStrategy = (strategyId: string) => apiGet<Strategy>(`/api/v1/strategies/${strategyId}`)
export const createStrategy = (name: string, objective: string, instrumentClass: string) =>
  apiPost<Strategy>('/api/v1/strategies', { name, objective, instrument_class: instrumentClass })
export const submitSuggestion = (strategyId: string, baseVersionId: string, suggestionText: string) =>
  apiPost<SuggestionDto>(`/api/v1/strategies/${strategyId}/suggestions`, {
    base_version_id: baseVersionId,
    suggestion_text: suggestionText,
  })
export const reviewSuggestion = (strategyId: string, suggestionId: string) =>
  apiPost<SuggestionDto>(`/api/v1/strategies/${strategyId}/suggestions/${suggestionId}/review`)
export const regenerateFromSuggestion = (strategyId: string, suggestionId: string) =>
  apiPost<SuggestionDto>(`/api/v1/strategies/${strategyId}/suggestions/${suggestionId}/regenerate`)
export const requestPromotion = (strategyId: string) =>
  apiPost<ApprovalRequestDto>(`/api/v1/strategies/${strategyId}/request-promotion`)
export const promoteStrategy = (strategyId: string) =>
  apiPost<Strategy>(`/api/v1/strategies/${strategyId}/promote`)
export const requestLiveEligibility = (strategyId: string) =>
  apiPost<ApprovalRequestDto>(`/api/v1/strategies/${strategyId}/request-live-eligibility`)
export const approveLiveEligibility = (strategyId: string, body: GoLiveReadinessInput) =>
  apiPost<Strategy>(`/api/v1/strategies/${strategyId}/approve-live-eligibility`, body)
export const checkGoLiveReadiness = (body: GoLiveReadinessInput) =>
  apiPost<GoLiveReadinessResult>('/api/v1/risk/go-live-readiness', body)

// ---- Backtests ----
export type BacktestMetrics = {
  cagr: number | null
  sharpe: number | null
  max_drawdown: number | null
  win_rate: number | null
  profit_factor: number | null
  num_trades: number
}
export type WalkForwardWindow = {
  train_start: string
  train_end: string
  test_start: string
  test_end: string
  out_of_sample_expectancy: number | null
  num_trades: number
  passed: boolean
}
export type WalkForwardResult = { windows: WalkForwardWindow[]; passed: boolean }
export type MonteCarloResult = {
  n_paths: number
  percentile_95_max_drawdown: number | null
  mean_final_pnl: number | null
  median_final_pnl: number | null
  worst_path_max_drawdown: number | null
}

export type BacktestRun = {
  id: string
  strategy_version_id: string
  symbol: string
  start_date: string
  end_date: string
  status: string
  refusal_reason: string | null
  metrics: BacktestMetrics | null
  daily_returns: [string, number][] | null
  trade_pnls: number[] | null
  walk_forward_result: WalkForwardResult | null
  monte_carlo_result: MonteCarloResult | null
  created_at: string
}

export type ComparisonResult = { run_ids: string[]; correlations: Record<string, number> }

export const listBacktests = (strategyVersionId?: string) =>
  apiGet<BacktestRun[]>(
    `/api/v1/backtests${strategyVersionId ? `?strategy_version_id=${strategyVersionId}` : ''}`,
  )
export const getBacktest = (runId: string) => apiGet<BacktestRun>(`/api/v1/backtests/${runId}`)
export const runMonteCarlo = (runId: string, nPaths = 1000, seed?: number) =>
  apiPost<BacktestRun>(`/api/v1/backtests/${runId}/monte-carlo`, { n_paths: nPaths, seed })
export const compareBacktests = (runIds: string[]) =>
  apiPost<ComparisonResult>('/api/v1/backtests/compare', { run_ids: runIds })

// ---- Audit log ----
export type AuditLogEntry = {
  id: string
  sequence: number
  previous_hash: string
  hash: string
  actor: string
  action: string
  entity_type: string
  entity_id: string
  details: Record<string, unknown> | null
  correlation_id: string | null
  created_at: string
}

export type AuditChainVerifyResult = {
  diverged: boolean
  archive_internally_valid: boolean
  live_db_matches_archive: boolean
  first_diverged_sequence: number | null
  reason: string | null
  entries_checked_in_db_chain: number
  db_chain_valid: boolean
  db_chain_reason: string | null
}

export const listAuditEntries = (params: { entityType?: string; entityId?: string; actor?: string; limit?: number; offset?: number } = {}) => {
  const query = new URLSearchParams()
  if (params.entityType) query.set('entity_type', params.entityType)
  if (params.entityId) query.set('entity_id', params.entityId)
  if (params.actor) query.set('actor', params.actor)
  query.set('limit', String(params.limit ?? 200))
  query.set('offset', String(params.offset ?? 0))
  return apiGet<AuditLogEntry[]>(`/api/v1/audit/entries?${query.toString()}`)
}
export const verifyAuditChain = () => apiPost<AuditChainVerifyResult>('/api/v1/audit/verify')

export async function downloadAuditExport(format: 'csv' | 'ndjson'): Promise<void> {
  const token = getAccessToken()
  const res = await fetch(`${API_BASE_URL}/api/v1/audit/export?format=${format}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (!res.ok) throw new ApiError(res.status, 'Export failed')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `audit_log_export.${format === 'ndjson' ? 'ndjson' : 'csv'}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

// ---- Unified orders (paper + live) ----
export type UnifiedExecution = {
  id: string
  mode: 'paper' | 'live'
  time: string
  symbol: string
  side: string
  quantity: number
  price: number | null
  status: string
  strategy_id: string | null
}

export const listOrders = (mode: 'paper' | 'live' | 'both' = 'both') =>
  apiGet<UnifiedExecution[]>(`/api/v1/orders?mode=${mode}`)

export type PositionByStrategy = {
  mode: 'paper' | 'live'
  strategy_id: string
  strategy_name: string
  symbol: string
  quantity: number
  avg_cost: number
  realized_pnl: number
}

export const listPositionsByStrategy = () => apiGet<PositionByStrategy[]>('/api/v1/orders/positions')

// ---- Portfolio (Phase 24, docs/phase20-old-vs-new-comparison.md items 22
// and 23): advisory-only rebalancing recommendations (each allocation
// action is decided from real backtest metrics, never fabricated; a
// human accepts/rejects -- nothing here ever places an order) plus a
// single cross-broker-ready dashboard rollup. ----
export type PortfolioAllocationEntry = {
  strategy_id: string
  strategy_name: string
  strategy_status: string
  action: 'increase' | 'decrease' | 'hold'
  rationale: string
  sharpe: number | null
  max_drawdown: number | null
}
export type PortfolioRecommendation = {
  id: string
  summary: string
  allocations: PortfolioAllocationEntry[]
  based_on: Record<string, unknown>
  llm_source: 'llm' | 'fallback'
  status: 'pending' | 'accepted' | 'rejected'
  reviewed_by: string | null
  reviewed_at: string | null
  reviewer_notes: string | null
  created_at: string
}
export const generatePortfolioRecommendation = () =>
  apiPost<PortfolioRecommendation>('/api/v1/portfolio/recommendations/generate')
export const listPortfolioRecommendations = () =>
  apiGet<PortfolioRecommendation[]>('/api/v1/portfolio/recommendations')
export const getPortfolioRecommendation = (id: string) =>
  apiGet<PortfolioRecommendation>(`/api/v1/portfolio/recommendations/${id}`)
export const acceptPortfolioRecommendation = (id: string, notes?: string) =>
  apiPost<PortfolioRecommendation>(`/api/v1/portfolio/recommendations/${id}/accept`, { notes })
export const rejectPortfolioRecommendation = (id: string, notes?: string) =>
  apiPost<PortfolioRecommendation>(`/api/v1/portfolio/recommendations/${id}/reject`, { notes })

export type PortfolioSummary = {
  as_of: string
  active_strategy_count: number
  paper_realized_pnl_today: number
  paper_open_position_count: number
  live_position_count: number
  broker_configured: boolean
  broker_name: string | null
  available_margin: number | null
  used_margin: number | null
}
export const getPortfolioSummary = () => apiGet<PortfolioSummary>('/api/v1/portfolio/summary')

export type PortfolioExposureEntry = {
  mode: 'paper' | 'live'
  strategy_id: string
  strategy_name: string
  symbol: string
  exposure: number
}
export type PortfolioRiskMetrics = {
  as_of: string
  open_position_count: number
  total_exposure: number
  exposure_by_position: PortfolioExposureEntry[]
  largest_position_concentration_pct: number | null
  broker_configured: boolean
  margin_utilization_pct: number | null
}
export const getPortfolioRiskMetrics = () =>
  apiGet<PortfolioRiskMetrics>('/api/v1/portfolio/risk-metrics')

// ---- Manual order intent (routed through the real risk gate) ----
export type OrderIntentInput = {
  mode: KillSwitchMode
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  proposed_price: number
  reference_price: number
  proposed_position_value: number
  portfolio_value: number
}
export type OrderIntentResult = {
  mode: string
  symbol: string
  side: string
  quantity: number
  price: number
  created_at: string
}
export const submitOrderIntent = (body: OrderIntentInput) =>
  apiPost<OrderIntentResult>('/api/v1/risk/order-intents', body)

// ---- Agent Gateway config (Settings + Agent Fleet enable/disable/skills/heartbeat) ----
export type GatewayConfigResponse = {
  raw_text: string
  parsed: Record<string, unknown>
  version_id: number | null
}
export type ApplyResult = { status: string; version_id: number; errors: string[] }
export type ConfigVersionSummary = {
  id: number
  status: string
  source: string
  created_at: string
  validation_errors: unknown
}

export const getGatewayConfig = () => apiGet<GatewayConfigResponse>('/api/v1/gateway/config')
export const putGatewayConfig = (rawText: string) =>
  apiPut<ApplyResult>('/api/v1/gateway/config', { raw_text: rawText })
export const validateGatewayConfig = (rawText: string) =>
  apiPost<{ ok: boolean; errors: string[] }>('/api/v1/gateway/config/validate', { raw_text: rawText })
export const listGatewayConfigVersions = () =>
  apiGet<ConfigVersionSummary[]>('/api/v1/gateway/config/versions')
export const rollbackGatewayConfig = (versionId: number) =>
  apiPost<ApplyResult>(`/api/v1/gateway/config/versions/${versionId}/rollback`)

// ---- Broker credentials (write-only) ----
export type BrokerTokenStatus = 'valid' | 'expired' | 'never-connected'
export type BrokerCredentialStatus = {
  broker: string
  configured: boolean
  token_status: BrokerTokenStatus
  token_expires_at: string | null
  // Always set by the backend (custom override, else the default) -- it has
  // to be registered with the broker before they'll issue an API key.
  redirect_uri: string | null
  default_redirect_uri?: string | null
  redirect_uri_is_custom?: boolean
  token_duration: string | null
}
export const KNOWN_BROKERS = ['zerodha', 'upstox'] as const
export const listBrokerCredentialStatus = () => apiGet<BrokerCredentialStatus[]>('/api/v1/broker-credentials')
export type BrokerCircuitBreakerStatus = {
  broker: string
  state: 'closed' | 'open'
  consecutive_failures: number
  failure_threshold: number
  cooldown_remaining_seconds: number | null
}
export const listBrokerCircuitBreakerStatus = () =>
  apiGet<BrokerCircuitBreakerStatus[]>('/api/v1/broker-credentials/circuit-breaker')
export const writeBrokerCredentials = (broker: string, apiKey: string, apiSecret?: string, accessToken?: string, redirectUri?: string) =>
  apiPost<void>(`/api/v1/broker-credentials/${broker}`, {
    api_key: apiKey,
    api_secret: apiSecret || null,
    access_token: accessToken || null,
    ...(redirectUri ? { redirect_uri: redirectUri } : {}),
  })
// null resets to the default callback URL.
export const writeBrokerRedirectUri = (broker: string, redirectUri: string | null) =>
  apiPut<void>(`/api/v1/broker-credentials/${broker}/redirect-uri`, { redirect_uri: redirectUri })
// Shown only if the status call itself failed -- the backend's value wins.
export const fallbackBrokerRedirectUri = (broker: string) =>
  `${API_BASE_URL}/api/v1/broker-credentials/${broker}/callback`
export const deleteBrokerCredentials = (broker: string) => apiDelete<void>(`/api/v1/broker-credentials/${broker}`)

// ---- Broker OAuth (real Zerodha/Upstox login completion) ----
export type BrokerOAuthLoginUrl = { login_url: string; redirect_uri: string }
export const getBrokerLoginUrl = (broker: string, duration?: 'standard' | 'extended') =>
  apiGet<BrokerOAuthLoginUrl>(
    `/api/v1/broker-credentials/${broker}/login-url${duration ? `?duration=${duration}` : ''}`
  )

// ---- LLM provider credentials (write-only) + real test/discovery ----
export type LlmProviderName =
  | 'anthropic'
  | 'openai'
  | 'gemini'
  | 'deepseek'
  | 'ollama'
  | 'custom'
  | 'huggingface'
export const LLM_PROVIDERS: LlmProviderName[] = [
  'anthropic',
  'openai',
  'gemini',
  'deepseek',
  'huggingface',
  'ollama',
  'custom',
]
export const LLM_PROVIDER_LABELS: Record<LlmProviderName, string> = {
  anthropic: 'Anthropic',
  openai: 'OpenAI',
  gemini: 'Google Gemini',
  deepseek: 'DeepSeek',
  ollama: 'Ollama (local)',
  custom: 'Custom / Local',
  huggingface: 'Hugging Face',
}
export type LlmProviderStatus = {
  provider: string
  configured: boolean
  base_url: string | null
  in_fallback_order: boolean
  // What an agent's "auto" model resolves to here: the chosen one, else the
  // built-in default (null for Ollama/Custom/Hugging Face, which need one).
  default_model?: string | null
  builtin_default_model?: string | null
}
export type LlmProviderTestResult = {
  provider: string
  ok: boolean
  detail: string
  latency_ms: number | null
}
export type LlmProviderModel = { id: string; label: string | null }
export const listLlmProviderStatus = () =>
  apiGet<LlmProviderStatus[]>('/api/v1/settings/llm-providers')
export const writeLlmProviderCredentials = (provider: string, apiKey?: string, baseUrl?: string) =>
  apiPost<void>(`/api/v1/settings/llm-providers/${provider}`, {
    api_key: apiKey || null,
    base_url: baseUrl || null,
  })
// null clears the choice (falls back to the built-in default, if any).
export const setLlmProviderDefaultModel = (provider: string, model: string | null) =>
  apiPut<void>(`/api/v1/settings/llm-providers/${provider}/default-model`, { model })
export const deleteLlmProviderCredentials = (provider: string) =>
  apiDelete<void>(`/api/v1/settings/llm-providers/${provider}`)
export const testLlmProvider = (provider: string, model?: string) =>
  apiPost<LlmProviderTestResult>(
    `/api/v1/settings/llm-providers/${provider}/test${model ? `?model=${encodeURIComponent(model)}` : ''}`
  )
export const listLlmProviderModels = (provider: string) =>
  apiGet<{ provider: string; models: LlmProviderModel[] }>(
    `/api/v1/settings/llm-providers/${provider}/models`
  )

// ---- Notification channels (write-only secrets) ----
export type NotificationChannelName = 'telegram' | 'discord' | 'slack'
export type AlertLevel = 'kill-switch' | 'sign-off' | 'go-live' | 'daily'
export const NOTIFICATION_CHANNELS: NotificationChannelName[] = ['telegram', 'discord', 'slack']
export const ALERT_LEVELS: AlertLevel[] = ['kill-switch', 'sign-off', 'go-live', 'daily']
export type NotificationChannelStatus = {
  channel: string
  configured: boolean
  enabled: boolean
  allowed_sender_ids: string[]
  alert_levels: string[]
}
export type WriteNotificationChannelInput = {
  enabled: boolean
  bot_token?: string | null
  chat_id?: string | null
  webhook_url?: string | null
  webhook_secret_token?: string | null
  public_key?: string | null
  signing_secret?: string | null
  allowed_sender_ids?: string[]
  alert_levels: string[]
}
export const listNotificationChannels = () => apiGet<NotificationChannelStatus[]>('/api/v1/notification-channels')
export const writeNotificationChannel = (channel: string, body: WriteNotificationChannelInput) =>
  apiPost<void>(`/api/v1/notification-channels/${channel}`, body)
export const deleteNotificationChannel = (channel: string) => apiDelete<void>(`/api/v1/notification-channels/${channel}`)
export type TestNotificationChannelResult = {
  channel: string
  ok: boolean
  status_code: number | null
  error: string | null
  tested_at: string
}
export const testNotificationChannel = (
  channel: string,
  overrides?: { bot_token?: string; chat_id?: string; webhook_url?: string }
) =>
  apiPost<TestNotificationChannelResult>(`/api/v1/notification-channels/${channel}/test`, {
    bot_token: overrides?.bot_token || null,
    chat_id: overrides?.chat_id || null,
    webhook_url: overrides?.webhook_url || null,
  })
export type DetectTelegramChatIdResult = {
  ok: boolean
  chat_id: string | null
  chat_label: string | null
  detail: string
}
export const detectTelegramChatId = (botToken: string) =>
  apiPost<DetectTelegramChatIdResult>('/api/v1/notification-channels/telegram/detect-chat-id', {
    bot_token: botToken,
  })

// ---- Dual-control risk limits ----
export type RiskLimit = { name: string; value: number }
export type RiskLimitChange = {
  id: string
  limit_name: string
  proposed_value: number
  reason: string | null
  status: string
  staged_by: string
  staged_at: string
  confirmed_by: string | null
  confirmed_at: string | null
  applied_by: string | null
  applied_at: string | null
}
export const listRiskLimits = () => apiGet<RiskLimit[]>('/api/v1/risk-limits')
export const listRiskLimitChanges = (statusFilter?: string) =>
  apiGet<RiskLimitChange[]>(`/api/v1/risk-limits/changes${statusFilter ? `?status_filter=${statusFilter}` : ''}`)
export const stageRiskLimitChange = (limitName: string, proposedValue: number, reason?: string) =>
  apiPost<RiskLimitChange>('/api/v1/risk-limits/stage', { limit_name: limitName, proposed_value: proposedValue, reason })
export const confirmRiskLimitChange = (changeId: string) =>
  apiPost<RiskLimitChange>(`/api/v1/risk-limits/${changeId}/confirm`)
export const applyRiskLimitChange = (changeId: string) =>
  apiPost<RiskLimitChange>(`/api/v1/risk-limits/${changeId}/apply`)

// ---- Market hours ----
export type MarketHours = { is_open: boolean; as_of: string }
export const getMarketHours = () => apiGet<MarketHours>('/api/v1/market-data/market-hours')

// ---- Market data: pulse, freshness, instruments ----
export type MarketPulse = {
  as_of: string
  india_vix: number
  sector_indices_change_pct: Record<string, number>
  global_indices_change_pct: Record<string, number>
}
export const getMarketPulse = () => apiGet<MarketPulse>('/api/v1/market-data/pulse')

export type FreshnessRecord = {
  symbol: string
  data_type: string
  data_date: string
  row_count: number
  ingested_at: string
}
export const getFreshness = (symbol: string) => apiGet<FreshnessRecord[]>(`/api/v1/market-data/freshness/${symbol}`)

export type IndicatorSeries = {
  symbol: string
  dates: string[]
  close: number[]
  sma: (number | null)[]
  sma_window: number
  ema: (number | null)[]
  ema_span: number
  rsi: (number | null)[]
  rsi_period: number
  macd: (number | null)[]
  macd_signal: (number | null)[]
  macd_histogram: (number | null)[]
  bollinger_upper: (number | null)[]
  bollinger_middle: (number | null)[]
  bollinger_lower: (number | null)[]
}
export const getIndicators = (symbol: string, lookbackDays = 250) =>
  apiGet<IndicatorSeries>(`/api/v1/market-data/indicators/${symbol}?lookback_days=${lookbackDays}`)

// ---- Data lake provenance + status (Phase 23,
// docs/phase20-old-vs-new-comparison.md item 35) -- `/provenance` is the
// raw per-run log; `/datalake/status` composes it (latest run + latest
// SUCCESSFUL run per real pipeline, never silently omitting one that has
// never run) with lake-wide symbol coverage into the single "is the lake
// healthy right now" view neither `/provenance` nor per-symbol
// `/freshness/{symbol}` answers alone. ----
export type MarketDataProvenanceEntry = {
  id: string
  pipeline: string
  source: string
  status: string
  symbols_processed: number
  rows_ingested: number
  error_message: string | null
  details: Record<string, unknown> | null
  started_at: string
  completed_at: string
}
export const listProvenance = () =>
  apiGet<MarketDataProvenanceEntry[]>('/api/v1/market-data/provenance')

export type PipelineStatusEntry = {
  pipeline: string
  last_run_status: string | null
  last_run_at: string | null
  last_run_source: string | null
  last_error: string | null
  last_success_at: string | null
}
export type DatalakeStatus = {
  as_of: string
  pipelines: PipelineStatusEntry[]
  symbols_with_daily_data: number
  symbols_with_intraday_data: number
  most_recent_daily_data_date: string | null
  most_recent_intraday_data_date: string | null
}
export const getDatalakeStatus = () =>
  apiGet<DatalakeStatus>('/api/v1/market-data/datalake/status')

export type Instrument = {
  symbol: string
  exchange: string
  instrument_type: string
  isin: string | null
  lot_size: number
  tick_size: number
  underlying_symbol: string | null
  expiry_date: string | null
  strike_price: number | null
  option_type: string | null
}
export const listInstruments = () => apiGet<Instrument[]>('/api/v1/market-data/instruments')

// ---- System vitals (Phase 17 real-world testing pass) ----
export type LlmProviderHealthVitals = {
  provider: string
  last_failure_at: string | null
  last_success_at: string | null
  served_as_fallback: boolean
}
export type SystemVitals = {
  host: { cpu_percent: number; memory_percent: number; memory_used_mb: number; uptime_seconds: number }
  llm: {
    active_provider: string | null
    token_usage_today: Record<string, number>
    provider_health: LlmProviderHealthVitals[]
  }
  latency: {
    order_dispatch_p50_ms: number | null
    order_dispatch_p95_ms: number | null
    window: string
    budget_ms: number
  }
  market: { is_open: boolean; next_event_at: string }
  as_of: string
}
export const getSystemVitals = () => apiGet<SystemVitals>('/api/v1/system/vitals')

// ---- Live option chain (Phase 16 audit follow-up D) ----
export type LiveOptionChainEntry = {
  strike: number
  call_symbol: string | null
  put_symbol: string | null
  call_ltp: number | null
  put_ltp: number | null
  call_oi: number | null
  put_oi: number | null
  call_iv: number | null
  put_iv: number | null
  call_iv_computed: number | null
  put_iv_computed: number | null
}
export type LiveOptionChain = {
  broker: string
  underlying: string
  expiry: string
  underlying_ltp: number | null
  atm_strike: number | null
  entries: LiveOptionChainEntry[]
}
export const getLiveOptionChain = (underlying: string, expiry: string) =>
  apiGet<LiveOptionChain>(
    `/api/v1/market-data/option-chain/${encodeURIComponent(underlying)}?expiry=${encodeURIComponent(expiry)}`
  )
export const getLiveOptionExpiries = (underlying: string) =>
  apiGet<string[]>(`/api/v1/market-data/option-expiries/${encodeURIComponent(underlying)}`)

// ---- Investor Reports (Phase 19, docs/phase19-audit.md Part 2.5/3.3) --
// real markdown narratives from src.orchestration.investor_reporting,
// browsable here, plus a manual "generate now" trigger for ops. ----
export type InvestorReportSummary = {
  id: string
  period_start: string
  period_end: string
  cadence: string
  generated_at: string
}
export type InvestorReport = InvestorReportSummary & {
  content_markdown: string
  generated_by: string
}
export const listInvestorReports = () => apiGet<InvestorReportSummary[]>('/api/v1/investor-reports')
export const getInvestorReport = (reportId: string) =>
  apiGet<InvestorReport>(`/api/v1/investor-reports/${reportId}`)
export const generateInvestorReport = (input: {
  period_start: string
  period_end: string
  cadence?: string
}) => apiPost<InvestorReport>('/api/v1/investor-reports/generate', input)

// ---- Chat (Phase 22, docs/phase20-old-vs-new-comparison.md item 16) ----
// The backend (src/api/routes/chat.py) has been real since well before this
// page existed -- session CRUD, search, pin, per-session model switch,
// streaming replies over SSE, abort, export. This is a pure frontend build
// over an API that needed nothing added.
export type ChatSession = {
  id: string
  title: string
  model: string | null
  pinned: boolean
  created_by: string
  created_at: string
  updated_at: string
}
export type ChatMessage = {
  id: string
  session_id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  provider: string | null
  aborted: boolean
  created_at: string
}
export const listChatSessions = (params?: { search?: string; pinnedOnly?: boolean }) => {
  const qs = new URLSearchParams()
  if (params?.search) qs.set('search', params.search)
  if (params?.pinnedOnly) qs.set('pinned_only', 'true')
  const suffix = qs.toString() ? `?${qs.toString()}` : ''
  return apiGet<ChatSession[]>(`/api/v1/chat/sessions${suffix}`)
}
export const createChatSession = (input: { title?: string; model?: string }) =>
  apiPost<ChatSession>('/api/v1/chat/sessions', input)
export const updateChatSession = (
  sessionId: string,
  input: { title?: string; model?: string; clear_model?: boolean; pinned?: boolean }
) => apiPatch<ChatSession>(`/api/v1/chat/sessions/${sessionId}`, input)
export const deleteChatSession = (sessionId: string) =>
  apiDelete<void>(`/api/v1/chat/sessions/${sessionId}`)
export const listChatMessages = (sessionId: string) =>
  apiGet<ChatMessage[]>(`/api/v1/chat/sessions/${sessionId}/messages`)
export const abortChatMessage = (sessionId: string) =>
  apiPost<void>(`/api/v1/chat/sessions/${sessionId}/abort`)
export const chatExportUrl = (sessionId: string, format: 'markdown' | 'json' = 'markdown') =>
  `${API_BASE_URL}/api/v1/chat/sessions/${sessionId}/export?format=${format}`

export type ChatStreamChunk = { text: string; done: boolean; provider: string | null }

/** Consumes `POST .../messages`'s `text/event-stream` body directly (no
 * `EventSource`: it can't send a POST body or an Authorization header) --
 * this codebase's first streaming consumer on the frontend, so this
 * function owns the raw fetch + reader loop rather than going through
 * `request()`, which only ever returns parsed JSON. One 401-refresh retry,
 * mirroring `request()`'s own logic, since a long-lived chat session is
 * exactly where an access token is likely to expire mid-use. */
export async function streamChatMessage(
  sessionId: string,
  content: string,
  onChunk: (chunk: ChatStreamChunk) => void,
  _isRetry = false
): Promise<void> {
  const token = getAccessToken()
  const res = await fetch(`${API_BASE_URL}/api/v1/chat/sessions/${sessionId}/messages`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ content }),
  })

  if (res.status === 401 && !_isRetry && token) {
    const refreshed = await refreshTokens()
    if (refreshed) return streamChatMessage(sessionId, content, onChunk, true)
  }
  if (!res.ok || !res.body) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      // no JSON body
    }
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail))
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let sepIndex: number
    // SSE frames are separated by a blank line ("\n\n"); each complete
    // frame in the buffer is parsed and removed, and any trailing partial
    // frame is left for the next chunk to complete.
    while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sepIndex)
      buffer = buffer.slice(sepIndex + 2)
      const line = frame.split('\n').find((l) => l.startsWith('data: '))
      if (line) onChunk(JSON.parse(line.slice(6)) as ChatStreamChunk)
    }
  }
}

export { API_BASE_URL }
