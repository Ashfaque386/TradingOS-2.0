// Thin fetch wrapper for the real TradingOS backend (Phase 13 frontend
// wiring). No swr/react-query/axios is installed, so this is deliberately
// plain `fetch` plus JWT storage + a single-flight refresh-on-401 retry --
// every page/component should go through here rather than calling
// `fetch` directly, so auth and error handling stay in one place.

import { DEFAULT_PARAMS, type IndicatorParams, indicatorQuery } from '@/lib/indicator-params'

const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

export class ApiError extends Error {
  status: number
  /** Seconds the server asked us to wait (`Retry-After`), when it said. */
  retryAfterSeconds?: number
  constructor(status: number, message: string, retryAfterSeconds?: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.retryAfterSeconds = retryAfterSeconds
  }
}

function retryAfterOf(res: Response): number | undefined {
  const seconds = Number(res.headers.get('Retry-After'))
  return Number.isFinite(seconds) && seconds > 0 ? seconds : undefined
}

// ---- Session ----
// The access token lives in this module's memory and nowhere else: not localStorage, not a cookie a
// script can read. It is gone when the tab reloads or closes, and the page gets a new one by presenting
// the refresh token -- which the browser holds as an HttpOnly cookie that no script can read, and sends
// only to /api/v1/auth (src/core/web_session.py). So a script injected into the page can use the session
// while the page is open, but cannot carry it away.
let accessToken: string | null = null

// Tokens an earlier version of the console kept in localStorage (the refresh token among them) are
// removed the first time this module loads, so they do not sit there for a script to find.
const LEGACY_TOKENS_KEY = 'tradingos.tokens'
if (typeof window !== 'undefined') {
  try {
    window.localStorage.removeItem(LEGACY_TOKENS_KEY)
  } catch {
    // storage unavailable: nothing could have been kept there either
  }
}

export function getAccessToken(): string | null {
  return accessToken
}

export function isAuthenticated(): boolean {
  return accessToken !== null
}

// Concurrent 401s must not each fire their own refresh (that would race the backend's single-use
// refresh-token rotation and revoke the whole family) -- every caller in flight shares the one
// in-progress refresh. A refresh can fail two very different ways, and treating them the same signs an
// operator out for no reason: the server looked at the refresh token and refused it (the session really
// is over -- forget it), or the server never got to judge it at all (rate-limited, down, network drop --
// the session may be fine, so let the caller retry).
export type RefreshOutcome =
  | { kind: 'ok' }
  | { kind: 'rejected' }
  | { kind: 'transient'; status: number }

let refreshPromise: Promise<RefreshOutcome> | null = null

async function postRefresh(): Promise<RefreshOutcome> {
  try {
    const res = await fetch(`${API_BASE_URL}/api/v1/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
    })
    if (res.status === 429 || res.status >= 500) {
      return { kind: 'transient', status: res.status }
    }
    if (!res.ok) {
      accessToken = null
      return { kind: 'rejected' }
    }
    const data = (await res.json()) as { access_token: string }
    accessToken = data.access_token
    return { kind: 'ok' }
  } catch {
    // fetch() itself rejected: no HTTP response at all (network down, or a response the browser
    // blocked, such as a 429 with no CORS headers).
    return { kind: 'transient', status: 0 }
  }
}

/** Buys a new access token with the refresh cookie. Tabs share one cookie and each refresh retires the
 * token it presents, so two tabs presenting the same one would look like a stolen token being replayed
 * and end the session: the Web Locks API makes them take turns, and the second presents the cookie the
 * first just received. */
export function refreshSession(): Promise<RefreshOutcome> {
  if (!refreshPromise) {
    // `async` because TypeScript 5.9 types the lock's result as a promise of the callback's own promise;
    // an async function flattens it, and runs the same under 5.7.
    const run: () => Promise<RefreshOutcome> =
      typeof navigator !== 'undefined' && navigator.locks
        ? async () => navigator.locks.request('tradingos-session-refresh', postRefresh)
        : postRefresh
    refreshPromise = run().finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

/** True when a failed API call says nothing about whether the session is
 * valid -- the server was rate-limiting, erroring, or unreachable -- so the
 * caller should retry rather than treat the user as signed out. `fetch()`
 * rejecting with a TypeError (no response at all) counts, as does an
 * ApiError with status 0, 429 or 5xx. A 401/403/404/422 is a real answer. */
export function isTransientError(err: unknown): boolean {
  if (err instanceof ApiError) return err.status === 0 || err.status === 429 || err.status >= 500
  return err instanceof TypeError
}

async function requestWithResponse<T>(
  path: string,
  init: RequestInit = {},
  allowRefresh = true,
): Promise<{ data: T; res: Response }> {
  const token = getAccessToken()
  const headers = new Headers(init.headers)
  if (!headers.has('Content-Type') && init.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  if (token) headers.set('Authorization', `Bearer ${token}`)

  const res = await fetch(`${API_BASE_URL}${path}`, { ...init, headers, credentials: 'include' })

  if (res.status === 401 && allowRefresh && token) {
    const refreshed = await refreshSession()
    if (refreshed.kind === 'ok') return requestWithResponse<T>(path, init, false)
    // The token expired but the refresh couldn't even be judged: surface
    // that as the transient failure it is, not as the original 401 (which
    // would read as "your session is over").
    if (refreshed.kind === 'transient') {
      throw new ApiError(refreshed.status, 'Session refresh is temporarily unavailable')
    }
  }

  if (!res.ok) {
    let detail: unknown = res.statusText
    try {
      const body = await res.json()
      detail = body?.detail ?? detail
    } catch {
      // no JSON body
    }
    throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail), retryAfterOf(res))
  }

  if (res.status === 204) return { data: undefined as T, res }
  const text = await res.text()
  return { data: (text ? JSON.parse(text) : undefined) as T, res }
}

async function request<T>(path: string, init: RequestInit = {}, allowRefresh = true): Promise<T> {
  return (await requestWithResponse<T>(path, init, allowRefresh)).data
}

export const apiGet = <T>(path: string) => request<T>(path, { method: 'GET' })

/** One page of a list, and how many rows match in all (the server's `X-Total-Count`; the page's own
 * length when the server did not say, which is the case for a list that is not paged). */
export type Page<T> = { items: T[]; total: number }
export async function apiGetPage<T>(path: string): Promise<Page<T>> {
  const { data, res } = await requestWithResponse<T[]>(path, { method: 'GET' })
  const header = res.headers.get('X-Total-Count')
  const total = header === null ? Number.NaN : Number(header)
  return { items: data, total: Number.isFinite(total) ? total : data.length }
}
export const apiPost = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: body !== undefined ? JSON.stringify(body) : undefined })
export const apiPut = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  request<T>(path, { method: 'PUT', body: body !== undefined ? JSON.stringify(body) : undefined, headers })
export const apiPatch = <T>(path: string, body?: unknown, headers?: Record<string, string>) =>
  request<T>(path, { method: 'PATCH', body: body !== undefined ? JSON.stringify(body) : undefined, headers })

/** The `If-Match` header of an edit to the gateway config: the version the person read. The server
 * refuses (409) an edit made on a version that has since been replaced, and one that names none (428). */
const onConfigVersion = (versionId: number) => ({ 'If-Match': `"${versionId}"` })
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
  const data = await request<{ access_token: string }>(
    '/api/v1/auth/login',
    { method: 'POST', body: JSON.stringify({ email, password }) },
    false,
  )
  accessToken = data.access_token
  return getMe()
}

// Only the very first account on a fresh system can be registered (it becomes the SystemAdministrator,
// src/api/routes/auth.py); after that an administrator creates accounts (Settings > Users), and this is
// refused with a 403. /auth/register returns the created user only (no tokens), so this signs in right
// after to hand back an authenticated session in one step -- the password never touches disk, here or
// on the backend: it's hashed immediately and only the hash is persisted.
export async function register(email: string, password: string): Promise<CurrentUser> {
  await request<{ id: string; email: string; role: Role; is_active: boolean }>(
    '/api/v1/auth/register',
    { method: 'POST', body: JSON.stringify({ email, password }) },
    false,
  )
  return login(email, password)
}

export type RegistrationStatus = { open: boolean; detail: string }

/** Whether registering is possible at all, so the sign-in page offers it only when it is. Public. */
export const getRegistrationStatus = () =>
  request<RegistrationStatus>('/api/v1/auth/registration-status', { method: 'GET' }, false)

export type SignInPolicy = {
  max_failed_attempts: number
  failure_window_minutes: number
  lockout_minutes: number
  access_token_minutes: number
  session_days: number
}

export const getSignInPolicy = () => apiGet<SignInPolicy>('/api/v1/auth/sign-in-policy')

/** A one-time ticket to open a WebSocket with (src/core/ws_ticket.py): bought with the access token,
 * good for seconds and for one connection, so the token itself never travels in a URL. */
export async function getWsTicket(): Promise<string> {
  const res = await apiPost<{ ticket: string; expires_in: number }>('/api/v1/auth/ws-ticket')
  return res.ticket
}

export async function getMe(): Promise<CurrentUser> {
  const data = await apiGet<{ id: string; email: string; role: Role; is_active: boolean }>(
    '/api/v1/auth/me',
  )
  return toCurrentUser(data)
}

export async function logout(): Promise<void> {
  accessToken = null
  try {
    // The server revokes the refresh token and tells the browser to drop the cookie.
    await fetch(`${API_BASE_URL}/api/v1/auth/logout`, { method: 'POST', credentials: 'include' })
  } catch {
    // best-effort: the token in memory is already gone either way
  }
}

// ---- Users (SystemAdministrator only; src/api/routes/admin.py) ----
export type AdminUser = {
  id: string
  email: string
  role: Role
  is_active: boolean
  /** Set while the account is locked after too many wrong passwords; a time in the past means the
   * lock has run out. */
  locked_until: string | null
}

export const listAdminUsers = () => apiGet<AdminUser[]>('/api/v1/admin/users')
export const createAdminUser = (email: string, password: string, role: Role) =>
  apiPost<AdminUser>('/api/v1/admin/users', { email, password, role })
export const changeAdminUserRole = (userId: string, role: Role) =>
  apiPatch<AdminUser>(`/api/v1/admin/users/${userId}/role`, { role })
export const deactivateAdminUser = (userId: string) =>
  apiPost<AdminUser>(`/api/v1/admin/users/${userId}/deactivate`)
export const reactivateAdminUser = (userId: string) =>
  apiPost<AdminUser>(`/api/v1/admin/users/${userId}/reactivate`)
export const unlockAdminUser = (userId: string) =>
  apiPost<AdminUser>(`/api/v1/admin/users/${userId}/unlock`)
export const revokeAdminUserSessions = (userId: string) =>
  apiPost<{ user_id: string; revoked_count: number }>(
    `/api/v1/admin/users/${userId}/revoke-sessions`,
  )

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
  // The gateway config version these settings are from; an edit sends it back (see `onConfigVersion`)
  config_version_id: number | null
}

export const getAgents = () => apiGet<AgentSummary[]>('/api/v1/agents')

// ---- One agent's settings, edited on the config version the page read ----
export type AgentSettingsPatch = {
  enabled?: boolean
  heartbeat_enabled?: boolean
  heartbeat_interval_minutes?: number
  skills?: string[]
}
export type AgentSettingsResult = ApplyResult & {
  // The settings that actually changed ("enabled", "skills", ...)
  changed: string[]
}
export const patchAgentSettings = (agentId: string, patch: AgentSettingsPatch, versionId: number) =>
  apiPatch<AgentSettingsResult>(`/api/v1/agents/${agentId}`, patch, onConfigVersion(versionId))

// ---- The skills the app ships with: a grant is a choice from this list, not a typed id ----
export type SkillCatalogEntry = {
  skill_id: string
  summary: string
  effect: 'read' | 'action'
  // false: a placeholder with the real interface that returns an empty result
  working: boolean
  granted_to: string[]
}
export const getSkillCatalog = () => apiGet<SkillCatalogEntry[]>('/api/v1/agents/skills')

// ---- What an agent is doing, in sentences built from what was recorded ----
export type NarrationLine = { kind: 'state' | 'heartbeat' | 'work' | 'gap'; text: string }
export type AgentNarration = { agent_id: string; as_of: string; lines: NarrationLine[] }
export const getAgentNarration = (agentId: string) =>
  apiGet<AgentNarration>(`/api/v1/agents/${agentId}/narration`)

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

export const putAgentIdentity = (agentId: string, body: SetAgentIdentityInput, versionId: number) =>
  apiPut<ApplyResult>(`/api/v1/agents/${agentId}/identity`, body, onConfigVersion(versionId))

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
  tripped_at?: string | null
  reset_at?: string | null
  reset_reason?: string | null
}

export const getKillSwitch = (mode: KillSwitchMode) =>
  apiGet<KillSwitchState>(`/api/v1/kill-switch/${mode}`)
export const resetKillSwitch = (mode: KillSwitchMode, reason?: string) =>
  apiPost<KillSwitchState>(`/api/v1/kill-switch/${mode}/reset`, { reason })

// ---- Equity and the automatic kill switch ----
export type EquitySnapshot = {
  taken_at: string
  // null: it could not be measured (an unpriced position, no starting capital); `note` says why
  equity: number | null
  capital_base: number | null
  realized_net: number | null
  unrealized: number | null
  open_positions: number
  unpriced_positions: number
  price_source: 'real' | 'synthetic' | 'none'
  peak_equity: number | null
  drawdown_pct: number | null
  note: string | null
}
export type EquityHistory = {
  mode: KillSwitchMode
  // oldest first
  snapshots: EquitySnapshot[]
  threshold_pct: number
}
export const getEquityHistory = (mode: KillSwitchMode, limit = 500) =>
  apiGet<EquityHistory>(`/api/v1/equity/${mode}?limit=${limit}`)

export type EvaluateKillSwitchResult = {
  outcome: 'ok' | 'tripped' | 'already_tripped' | 'cannot_evaluate'
  snapshot: EquitySnapshot
  kill_switch: KillSwitchState
}
// Measure the equity now and run the check the scheduler runs.
export const evaluateKillSwitch = (mode: KillSwitchMode) =>
  apiPost<EvaluateKillSwitchResult>(`/api/v1/kill-switch/${mode}/evaluate`)
// A drill: judge numbers an operator supplies. The threshold is for this check only and is not stored.
export const checkKillSwitchDrill = (
  mode: KillSwitchMode,
  body: { current_equity: number; peak_equity: number; threshold_pct?: number },
) => apiPost<KillSwitchState>(`/api/v1/kill-switch/${mode}/check`, body)

export type TodaysPaperPnl = {
  as_of_date: string
  // Gross of costs (the price difference only); `net_realized_pnl` is after the fills' costs.
  realized_pnl: number
  costs_total: number
  net_realized_pnl: number
  fill_count: number
}
export const getTodaysPaperPnl = () => apiGet<TodaysPaperPnl>('/api/v1/paper-trading/pnl/today')

export type UnrealizedPaperPnl = {
  as_of: string
  price_source: 'real' | 'synthetic' | 'none'
  unrealized_pnl: number | null
  positions_priced: number
  positions_unpriced: number
  // open positions on an inactive subscription: nothing watches their stop-loss
  unmanaged_positions: number
}
export const getUnrealizedPaperPnl = () =>
  apiGet<UnrealizedPaperPnl>('/api/v1/paper-trading/pnl/unrealized')

// ---- Paper-trading desk: subscriptions, legs, fills, controls ----
export type PaperInstrumentKind = 'equity' | 'future' | 'option'
// Derived by the backend: inactive; else awaiting_history (too little real price history to
// signal); else halted_no_data (no real price feed, or a futures contract past expiry); else active.
export type PaperState = 'active' | 'awaiting_history' | 'halted_no_data' | 'inactive'
export type PaperOptionStrategy = 'long_option' | 'vertical_spread'

export type PaperLastTick = {
  price: number
  age_seconds: number
  source: string
  // When the exchange stamped this price and how old that stamp is now; null where the broker sends
  // none (Upstox's is not yet verified): "not known", not "fresh".
  exchange_time?: string | null
  exchange_age_seconds?: number | null
  // The exchange's circuit limits for the day from the same quote; null when it carried none.
  lower_circuit?: number | null
  upper_circuit?: number | null
}

// The newest quote for a subscription was not acted on (its exchange stamp is from a previous day, or
// too old). Present only while the refusals continue.
export type PaperQuoteRefusal = { broker: string; reason: string; exchange_time: string | null; at: string }

export type PaperSubscription = {
  id: string
  strategy_version_id: string
  symbol: string
  builtin_strategy: string
  sma_window: number
  initial_capital: number
  stop_loss_pct: number
  position_size_pct: number
  is_active: boolean
  instrument_kind: PaperInstrumentKind
  contract_symbol: string | null
  contract_expiry: string | null
  lot_size: number | null
  lots: number
  option_strategy: PaperOptionStrategy | null
  spread_width: number
  open_legs: number
  state: PaperState
  state_reason: string | null
  created_at: string
  created_by: string | null
  deactivated_at: string | null
  deactivated_by: string | null
  deactivation_reason: string | null
  // equity/future: the one position; an option strategy holds legs (these are 0 across legs)
  quantity: number
  avg_cost: number
  // gross of costs; net = realized_pnl - costs_total
  realized_pnl: number
  costs_total: number
  net_realized_pnl: number
  // false for an open position on an inactive subscription: nothing watches its stop-loss
  managed: boolean
  last_tick: PaperLastTick | null
  unrealized_pnl: number | null
  quote_refusal?: PaperQuoteRefusal | null
  // The band (percent of the previous close) the risk gate falls back to when a quote carries no
  // exchange circuit limits: the reference table's, not an exchange figure.
  circuit_fallback_band_pct?: number | null
}

export type PaperPosition = {
  subscription_id: string
  symbol: string
  quantity: number
  avg_cost: number
  realized_pnl: number
  costs_total: number
  net_realized_pnl: number
  // open legs only; null = never priced (unknown, not zero)
  mark_price: number | null
  mark_age_seconds: number | null
  unrealized_pnl: number | null
}

export type PaperCostBreakdown = {
  segment?: string
  product?: string
  turnover?: number
  brokerage?: number
  stt?: number
  exchange_txn_charges?: number
  sebi_fee?: number
  stamp_duty?: number
  gst?: number
}

export type PaperFill = {
  id: string
  subscription_id: string
  symbol: string
  side: 'buy' | 'sell'
  order_group_id: string
  leg_index: number
  requested_quantity: number
  filled_quantity: number
  avg_fill_price: number | null
  fully_filled: boolean
  realized_pnl: number
  costs_total: number
  costs_breakdown: PaperCostBreakdown | null
  // real: a real tick on the broker's real depth; mixed: a generated book; supplied: an operator's price
  data_source: string
  // A close done by hand without the risk gate: not a trade the strategy made.
  administrative: boolean
  created_at: string
}

export type PaperClosed = {
  fill: PaperFill
  position: PaperPosition
  closed: boolean
  remaining_quantity: number
  price: number
  price_basis: string
  data_source: string
  administrative: boolean
  legs: PaperFill[]
  failures: string[]
}

export type EnrolPaperRequest = {
  strategy_version_id: string
  symbol: string
  instrument_kind: PaperInstrumentKind
  builtin_strategy?: 'always_long' | 'sma_crossover'
  sma_window?: number
  initial_capital?: number
  stop_loss_pct?: number
  position_size_pct?: number
  lots?: number
  option_strategy?: PaperOptionStrategy
  spread_width?: number
}

export const listPaperSubscriptions = (filters: { active?: boolean; state?: PaperState } = {}) => {
  const query = new URLSearchParams()
  if (filters.active !== undefined) query.set('active', String(filters.active))
  if (filters.state) query.set('state', filters.state)
  const suffix = query.toString()
  return apiGet<PaperSubscription[]>(
    `/api/v1/paper-trading/subscriptions${suffix ? `?${suffix}` : ''}`,
  )
}
export const enrolPaperSubscription = (body: EnrolPaperRequest) =>
  apiPost<{ id: string; symbol: string }>('/api/v1/paper-trading/subscriptions', body)
export const listPaperPositions = (subscriptionId: string) =>
  apiGet<PaperPosition[]>(`/api/v1/paper-trading/subscriptions/${subscriptionId}/positions`)
export const listPaperFills = (subscriptionId: string) =>
  apiGet<PaperFill[]>(`/api/v1/paper-trading/subscriptions/${subscriptionId}/fills`)

/** One buy or sell decision from the daily signal run on a subscription's price history. */
export type PaperSignal = {
  id: string
  subscription_id: string
  symbol: string
  signal_type: 'BUY' | 'SELL'
  /** The daily bar's own close, which the risk gate judges the order's price against. */
  reference_price: number
  /** True once a position was entered or exited on it. */
  consumed: boolean
  generated_at: string
  consumed_at: string | null
}
export const listPaperSignals = (subscriptionId: string) =>
  apiGet<PaperSignal[]>(`/api/v1/paper-trading/subscriptions/${subscriptionId}/signals`)

/** What fetching a subscription's real price history did, including a failure and its reason. */
export type PaperHistoryBackfill = {
  status: 'success' | 'failed'
  symbol: string
  rows: number
  first_date: string | null
  last_date: string | null
  source: string | null
  unreal_rows_removed: number
  error: string | null
}
export const backfillPaperHistory = (subscriptionId: string) =>
  apiPost<PaperHistoryBackfill>(`/api/v1/paper-trading/subscriptions/${subscriptionId}/backfill-history`)
export const deactivatePaperSubscription = (
  subscriptionId: string,
  body: { reason: string; on_open_position?: 'close' | 'leave_unmanaged'; close_price?: number },
) => apiPost<{ left_unmanaged_quantity: number }>(
  `/api/v1/paper-trading/subscriptions/${subscriptionId}/deactivate`,
  body,
)
export const activatePaperSubscription = (subscriptionId: string) =>
  apiPost<{ id: string }>(`/api/v1/paper-trading/subscriptions/${subscriptionId}/activate`)
export const closePaperPosition = (
  subscriptionId: string,
  body: { reason: string; price?: number; administrative?: boolean },
) => apiPost<PaperClosed>(
  `/api/v1/paper-trading/subscriptions/${subscriptionId}/close-position`,
  body,
)

// ---- Paper desk: previews and settings ----
export type PaperSymbolHit = { symbol: string; kind: 'equity' | 'index' }
// `available: false`: nothing to search (no broker connected); the caller types a symbol.
export type PaperSymbolSearch = { available: boolean; reason: string | null; symbols: PaperSymbolHit[] }

export type PaperOptionLegPreview = {
  symbol: string
  side: 'buy' | 'sell'
  option_type: 'CE' | 'PE'
  strike: number
  quantity: number
  price: number | null
}

export type PaperOptionScenario = {
  signal: 'BUY' | 'SELL'
  view: string
  legs: PaperOptionLegPreview[]
  // null: a leg has no price, so the largest loss is unknown (not zero)
  max_loss: number | null
  fits: boolean | null
  note: string | null
}

export type PaperEnrolPreview = {
  symbol: string
  instrument_kind: PaperInstrumentKind
  listing: 'listed' | 'not_listed' | 'unknown'
  price: number | null
  price_symbol: string | null
  equity: { entry_capital: number; shares: number | null; notional: number | null } | null
  future: {
    contract: string
    expiry: string
    rolls_on: string
    lot_size: number
    lots: number
    quantity: number
    notional: number | null
    margin: number | null
    margin_limit: number
    margin_pct: number
    fits: boolean | null
  } | null
  option: {
    expiry: string
    lot_size: number | null
    lots: number
    strategy: string
    spread_width: number
    limit: number
  } | null
  scenarios: PaperOptionScenario[]
  problems: string[] // the real enrolment would refuse
  warnings: string[] // it would allow, but the operator should know
}

export type PaperCloseLegPreview = {
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  avg_cost: number
  reference_price: number | null
  reference_age_seconds: number | null
  reference_source: string | null
  book_age_seconds: number | null
  // `real` (the broker's live depth) or `synthetic` (a generated book: a demo or test setup)
  book_source: string | null
  est_avg_price: number | null
  est_filled: number
  est_remaining: number
  est_costs: number | null
  est_gross_pnl: number | null
  problem: string | null
}

export type PaperClosePreview = {
  subscription_id: string
  symbol: string
  market_open: boolean
  legs: PaperCloseLegPreview[]
  blockers: string[]
  notes: string[]
  est_costs_total: number | null
  est_gross_pnl: number | null
  can_close: boolean
}

export type PaperSettingsRequest = {
  reason: string
  stop_loss_pct?: number
  position_size_pct?: number
  lots?: number
  spread_width?: number
  sma_window?: number
  builtin_strategy?: 'always_long' | 'sma_crossover'
  initial_capital?: number
}

export type PaperSettingChange = { field: string; before: number | string; after: number | string }

export const searchPaperSymbols = (query: string, limit = 8) =>
  apiGet<PaperSymbolSearch>(
    `/api/v1/paper-trading/symbols?q=${encodeURIComponent(query)}&limit=${limit}`,
  )
// A GET with the enrolment's own fields as query parameters: it changes nothing, so it leaves no
// audit row (a POST would write one for every preview).
export const previewPaperEnrolment = (body: Omit<EnrolPaperRequest, 'strategy_version_id'> & { strategy_version_id?: string }) => {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(body)) {
    if (value !== undefined) query.set(key, String(value))
  }
  return apiGet<PaperEnrolPreview>(`/api/v1/paper-trading/enrol-preview?${query.toString()}`)
}
export const previewPaperClose = (subscriptionId: string, price?: number) =>
  apiGet<PaperClosePreview>(
    `/api/v1/paper-trading/subscriptions/${subscriptionId}/close-preview${price !== undefined ? `?price=${price}` : ''}`,
  )
export const updatePaperSettings = (subscriptionId: string, body: PaperSettingsRequest) =>
  apiPatch<{ subscription: { id: string }; changes: PaperSettingChange[] }>(
    `/api/v1/paper-trading/subscriptions/${subscriptionId}/settings`,
    body,
  )

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

export type RunListQuery = {
  limit?: number
  offset?: number
  /** Only the runs still in progress (pending, planning or running), however old */
  active?: boolean
  status?: string
}
/** One page of runs, newest first, each with its tasks, and how many match in all. */
export const listRunsPage = (query: RunListQuery = {}) => {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset) params.set('offset', String(query.offset))
  if (query.active) params.set('active', 'true')
  if (query.status) params.set('status_filter', query.status)
  const suffix = params.toString()
  return apiGetPage<OrganizationRun>(`/api/v1/orchestration/runs${suffix ? `?${suffix}` : ''}`)
}
export const listRuns = async (query: RunListQuery = {}) => (await listRunsPage(query)).items
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
/** Stops a pending/planning/running/paused run for good (terminal -- Rerun
 * starts a fresh one). 409 if there was nothing to cancel (already finished). */
export const cancelRun = (runId: string) =>
  apiPost<OrganizationRun>(`/api/v1/orchestration/runs/${runId}/cancel`)

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
  // What the decider wrote when they approved or rejected; `reason` is why it was asked for.
  decision_reason?: string | null
  created_at: string
  // What the ids stand for, where known: the strategy's name, the people by e-mail.
  subject_label?: string | null
  requested_by_label?: string | null
  decided_by_label?: string | null
}

export const listApprovals = (statusFilter?: string) =>
  apiGet<ApprovalRequestDto[]>(`/api/v1/approvals${statusFilter ? `?status_filter=${statusFilter}` : ''}`)

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

export const listLiveIntents = (query: { statusFilter?: string; strategyId?: string; limit?: number; offset?: number } = {}) => {
  const params = new URLSearchParams()
  if (query.statusFilter) params.set('status_filter', query.statusFilter)
  if (query.strategyId) params.set('strategy_id', query.strategyId)
  params.set('limit', String(query.limit ?? 100))
  params.set('offset', String(query.offset ?? 0))
  return apiGetPage<LiveOrderIntentDto>(`/api/v1/live-trading/intents?${params.toString()}`)
}

/** One order a strategy sent to a broker: the broker's id for it, or why it failed. */
export type LiveOrderDto = {
  id: string
  live_order_intent_id: string
  strategy_id: string
  symbol: string
  side: string
  quantity: number
  broker_name: string
  broker_order_id: string | null
  status: string
  failure_reason: string | null
  submitted_at: string
}

/** A strategy's live orders, newest first. */
export const listStrategyLiveOrders = (strategyId: string, query: { limit?: number; offset?: number } = {}) =>
  apiGetPage<LiveOrderDto>(
    `/api/v1/live-trading/strategies/${strategyId}/orders?limit=${query.limit ?? 25}&offset=${query.offset ?? 0}`,
  )

/** One fill or attempt on a live order; `price` is the quote it was placed at, not a confirmed fill. */
export type LiveTradeDto = {
  id: string
  order_id: string
  symbol: string
  side: string
  quantity: number
  price: number
  status: string
  data_source: string
  executed_at: string
}

export const listLiveOrderTrades = (orderId: string) =>
  apiGet<LiveTradeDto[]>(`/api/v1/live-trading/orders/${orderId}/trades`)

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
/** Tighten a live subscription's limits (each may only get stricter), or switch it off for good. A
 * reason is required and audited. Administrator and risk manager only. */
export type UpdateLiveSubscriptionInput = {
  max_intents_per_window?: number
  rate_limit_window_minutes?: number
  max_notional_per_intent?: number
  intent_expiry_seconds?: number
  deactivate?: boolean
  reason: string
}
export type LiveLimitChange = { field: string; before: number; after: number }
export const updateLiveSubscription = (subscriptionId: string, body: UpdateLiveSubscriptionInput) =>
  apiPatch<{ subscription: LiveTradingSubscriptionDto; changes: LiveLimitChange[] }>(
    `/api/v1/live-trading/subscriptions/${subscriptionId}`,
    body,
  )
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

// ---- Agent pipeline live step feed (Phase 19 Finding #1) ----
// One event per step of one LangGraph pipeline run, from
// GET /api/v1/agents/pipeline/events (history) and the
// /ws/agent-pipeline-events channel (live). event_type is one of
// pipeline.started|completed|failed|cancelled and step.started|completed|failed|skipped
// (plus memory.recalled).
export type AgentPipelineEvent = {
  run_id: string
  sequence: number
  event_type: string
  node: string | null
  agent_id: string | null
  payload: Record<string, unknown> | null
  created_at: string
}
export type AgentPipelineEventMessage = { type: 'event'; event: AgentPipelineEvent }

export const listAgentPipelineEvents = (limit = 200, runId?: string) =>
  apiGet<AgentPipelineEvent[]>(
    `/api/v1/agents/pipeline/events?limit=${limit}${runId ? `&run_id=${runId}` : ''}`,
  )

export type RunPipelineResult = {
  objective: string
  node_log: string[]
  deployment_result: Record<string, unknown> | null
  evaluation_verdict: Record<string, unknown> | null
  rejection_count: number
  run_id: string
  /** True when the run was cancelled at a step boundary: the other fields then
   * describe only the steps that finished before it stopped. */
  cancelled: boolean
}

export const runAgentPipeline = (objective: string) =>
  apiPost<RunPipelineResult>('/api/v1/agents/pipeline/run', { objective })

/** Asks a running pipeline to stop. Cooperative: 202 means "requested" -- the
 * run notices within a moment and abandons the step in progress (its result
 * is discarded), and its own `pipeline.cancelled` event says it really
 * stopped. 409 if it had already finished. */
export const cancelAgentPipelineRun = (runId: string) =>
  apiPost<{ run_id: string; status: 'cancel_requested' }>(
    `/api/v1/agents/pipeline/runs/${runId}/cancel`,
  )

// ---- Agent long-term vector memory (docs/phase20-old-vs-new-comparison.md
// item 18) ----
// `available: false` is a normal, honest state (no Qdrant URL, or no embedding
// provider configured) -- `reason` says exactly what is missing, and the
// operations that need memory return 503 with the same reason.
export type MemoryKind = 'strategy' | 'news' | 'organization'
export type MemoryCollectionStatus = {
  kind: MemoryKind
  exists: boolean
  count: number
  dimension: number | null
}
export type MemoryStatus = {
  available: boolean
  reason: string | null
  qdrant_configured: boolean
  qdrant_reachable: boolean
  embedding_provider: string | null
  embedding_model: string | null
  kinds: MemoryKind[]
  collections: MemoryCollectionStatus[]
}
export type MemoryHit = {
  id: string
  kind: MemoryKind
  score: number
  text: string
  metadata: Record<string, unknown>
}

// The embedding provider/model, persisted in the database (not .env) so it
// survives restarts and rebuilds. `source` says where the effective value
// comes from: 'database' (saved here), 'environment' (MEMORY_EMBEDDING_*
// defaults) or 'unset'. Credentials are never part of this -- they stay in
// Settings > LLM providers.
export type MemorySettings = {
  provider: string
  model: string
  source: 'database' | 'environment' | 'unset'
  updated_by: string | null
  updated_at: string | null
  supported_providers: string[]
}

export const getMemorySettings = () => apiGet<MemorySettings>('/api/v1/memory/settings')
export const saveMemorySettings = (provider: string, model: string) =>
  apiPut<MemorySettings>('/api/v1/memory/settings', { provider, model })
export const resetMemorySettings = () => apiDelete<MemorySettings>('/api/v1/memory/settings')

export const getMemoryStatus = () => apiGet<MemoryStatus>('/api/v1/memory/status')

export const queryMemory = (q: string, kind?: MemoryKind, limit = 8) =>
  apiGet<MemoryHit[]>(
    `/api/v1/memory/query?q=${encodeURIComponent(q)}&limit=${limit}${kind ? `&kind=${kind}` : ''}`,
  )

export const rememberMemory = (kind: MemoryKind, text: string) =>
  apiPost<{ id: string; kind: MemoryKind }>(`/api/v1/memory/${kind}`, { text })

export const forgetMemory = (kind: MemoryKind, id: string) =>
  apiDelete<void>(`/api/v1/memory/${kind}/${id}`)

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
  // An administrator chose this schedule: it is kept across a restart. `default_trigger` is the
  // one the code registered the job with, which restoring puts back.
  overridden: boolean
  default_trigger: string | null
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

// Puts the job back on the schedule the code registered it with and forgets the kept choice
export const restoreScheduledJobSchedule = (scheduler: string, jobId: string) =>
  apiDelete<UpdateScheduledJobResponse>(
    `/api/v1/system/scheduled-jobs/${encodeURIComponent(scheduler)}/${encodeURIComponent(jobId)}/schedule`
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

// A version as the list of strategies carries it: no code, validation errors, sandbox result or
// options legs (a strategy read on its own, `getStrategy`, has them).
export type StrategyVersionSummary = Pick<
  StrategyVersion,
  'id' | 'version_number' | 'static_validation_passed' | 'sandbox_passed' | 'created_at'
>
export type StrategySummary = Omit<Strategy, 'versions'> & { versions: StrategyVersionSummary[] }

export type SuggestionDto = {
  id: string
  strategy_id: string
  base_version_id: string
  suggestion_text: string
  status: string
  ai_verdict: Record<string, unknown> | null
  regenerated_version_id: string | null
  regeneration_diff: string | null
  // Who asked and when it was asked and reviewed.
  requested_by: string | null
  created_at: string | null
  reviewed_at: string | null
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

export type StrategyListQuery = { limit?: number; offset?: number; status?: string }
/** One page of strategies, newest first, with a summary of each one's versions, and how many match. */
export const listStrategiesPage = (query: StrategyListQuery = {}) => {
  const params = new URLSearchParams()
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset) params.set('offset', String(query.offset))
  if (query.status) params.set('status_filter', query.status)
  const suffix = params.toString()
  return apiGetPage<StrategySummary>(`/api/v1/strategies${suffix ? `?${suffix}` : ''}`)
}
/** Up to the newest 500 strategies (the most the server gives at once), for the pickers that offer one. */
export const listStrategies = async () => (await listStrategiesPage({ limit: 500 })).items

/** Every improvement suggestion made for a strategy, newest first. */
export const listStrategySuggestions = (strategyId: string) =>
  apiGet<SuggestionDto[]>(`/api/v1/strategies/${strategyId}/suggestions`)
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
// Sign a strategy off for live eligibility. With no override the gate is judged on the evidence the
// system derived from its own records; an override puts typed numbers in their place, needs a reason,
// and is recorded in the audit trail beside the derived figures.
export type GoLiveOverride = GoLiveReadinessInput & { reason: string }
export const approveLiveEligibility = (strategyId: string, override?: GoLiveOverride) =>
  apiPost<Strategy>(`/api/v1/strategies/${strategyId}/approve-live-eligibility`, override ? { override } : {})
export type GoLiveEvidence = {
  strategy_id: string
  as_of: string
  inputs: Required<GoLiveReadinessInput>
  thresholds: {
    min_trades: number
    min_calendar_days: number
    min_clean_shadow_days: number
    max_win_rate_divergence_pp: number
  }
  eligible: boolean
  checks: Record<string, boolean>
  reasons: string[]
  counted_closing_fills: number
  counted_fills: number
  excluded_unreal_fills: number
  excluded_administrative_fills: number
  first_counted_fill_at: string | null
  backtest_run_id: string | null
  backtest_data_source: string | null
  shadow_clean_days_real_sandbox: number
  shadow_runs_failed_recently: number
  // inputs the records could not support, and why
  not_derived: Array<{ input: string; reason: string }>
}
export const getGoLiveEvidence = (strategyId: string) =>
  apiGet<GoLiveEvidence>(`/api/v1/strategies/${strategyId}/go-live-evidence`)

// ---- Shadow Mode (dry-run order checks) ----
export type ShadowModeRun = {
  id: string
  broker_name: string
  has_sandbox: boolean
  // real_sandbox_dry_run: the broker's own sandbox answered; local_payload_only: the order was built, nothing sent
  confidence: 'real_sandbox_dry_run' | 'local_payload_only'
  symbol: string
  side: string
  quantity: number
  created_at: string
  strategy_id: string | null
  outcome: 'ok' | 'failed'
  failure_reason: string | null
}
export const listShadowModeRuns = (strategyId?: string) =>
  apiGet<ShadowModeRun[]>(`/api/v1/shadow-mode/runs${strategyId ? `?strategy_id=${strategyId}` : ''}`)
export const runShadowModeCheck = (body: {
  broker: string
  symbol: string
  side: 'buy' | 'sell'
  quantity: number
  strategy_id?: string
}) => apiPost<ShadowModeRun>('/api/v1/shadow-mode/check', body)
export const checkGoLiveReadiness = (body: GoLiveReadinessInput) =>
  apiPost<GoLiveReadinessResult>('/api/v1/risk/go-live-readiness', body)

// ---- Backtests ----
export type BuiltinStrategy = 'sma_crossover' | 'always_long'

/** One trade of a run: when it opened and closed, at what, how many shares, and what it earned and cost. */
export type BacktestTrade = {
  entry_date: string
  exit_date: string
  entry_price: number
  exit_price: number
  quantity: number
  gross_pnl: number
  costs: number
  net_pnl: number
}

/** How a run's signals were made; `source` says whether the prices came from the data lake or a caller. */
export type BacktestSignalConfig = {
  strategy: BuiltinStrategy
  sma_window: number
  is_delivery: boolean
  initial_capital: number
  source: 'lake' | 'supplied'
}

export type BacktestMetrics = {
  total_return: number | null
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
  // Where the bars came from: 'supplied' means the caller posted them, so nobody here can
  // vouch they are real market data. 'unknown' for runs from before this was recorded.
  data_source?: 'real' | 'synthetic' | 'supplied' | 'mixed' | 'unknown'
  // Each trade with its dates; null for a run from before they were kept.
  trades: BacktestTrade[] | null
  signal_config: BacktestSignalConfig | null
  created_at: string
}

// null: fewer than 10 overlapping days, so there is no honest number to show
export type ComparisonResult = { run_ids: string[]; correlations: Record<string, number | null> }

export const listBacktests = (strategyVersionId?: string) =>
  apiGet<BacktestRun[]>(
    `/api/v1/backtests${strategyVersionId ? `?strategy_version_id=${strategyVersionId}` : ''}`,
  )
export const runMonteCarlo = (runId: string, nPaths = 1000, seed?: number) =>
  apiPost<BacktestRun>(`/api/v1/backtests/${runId}/monte-carlo`, { n_paths: nPaths, seed })
export const compareBacktests = (runIds: string[]) =>
  apiPost<ComparisonResult>('/api/v1/backtests/compare', { run_ids: runIds })

/** A backtest on the daily prices the data lake holds, between two dates. */
export type LakeBacktestRequest = {
  strategy_version_id: string
  symbol: string
  start_date: string
  end_date: string
  strategy?: BuiltinStrategy
  sma_window?: number
  is_delivery?: boolean
  initial_capital?: number
}
export const createBacktestFromLake = (body: LakeBacktestRequest) =>
  apiPost<BacktestRun>('/api/v1/backtests/from-lake', body)

/** A walk-forward on a run's own window, read from the lake. Strategy and average default to the run's. */
export type WalkForwardRequest = {
  train_bars: number
  test_bars: number
  step_bars?: number
  strategy?: BuiltinStrategy
  sma_window?: number
}
export const runWalkForward = (runId: string, body: WalkForwardRequest) =>
  apiPost<BacktestRun>(`/api/v1/backtests/${runId}/walk-forward`, body)

/** A search for the best moving-average length on a symbol's history in the lake. */
export type OptimizeRequest = {
  strategy_version_id: string
  symbol: string
  start_date: string
  end_date: string
  n_trials?: number
  objective_metric?: 'sharpe' | 'total_return' | 'cagr'
  sma_window_min?: number
  sma_window_max?: number
  seed?: number
}
export type OptimizationResult = {
  id: string
  n_trials: number
  best_params: Record<string, number | string> | null
  best_value: number | null
  param_importance: Record<string, number> | null
}
export const optimizeBacktest = (body: OptimizeRequest) =>
  apiPost<OptimizationResult>('/api/v1/backtests/optimize', body)

// ---- Audit log ----
export type AuditLogEntry = {
  id: string
  sequence: number
  previous_hash: string
  hash: string
  actor: string
  action: string
  entity_type: string | null
  entity_id: string | null
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

/** What narrows the log; each field is optional and they combine. `since` and `until` are ISO times. */
export type AuditQuery = {
  entityType?: string
  entityId?: string
  actor?: string
  action?: string
  search?: string
  since?: string
  until?: string
}

function auditQueryString(query: AuditQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.entityType) params.set('entity_type', query.entityType)
  if (query.entityId) params.set('entity_id', query.entityId)
  if (query.actor) params.set('actor', query.actor)
  if (query.action) params.set('action', query.action)
  if (query.search) params.set('search', query.search)
  if (query.since) params.set('since', query.since)
  if (query.until) params.set('until', query.until)
  return params
}

export const listAuditEntries = (query: AuditQuery & { limit?: number; offset?: number } = {}) => {
  const params = auditQueryString(query)
  params.set('limit', String(query.limit ?? 100))
  params.set('offset', String(query.offset ?? 0))
  return apiGetPage<AuditLogEntry>(`/api/v1/audit/entries?${params.toString()}`)
}

/** The values the log's filters can be set to; `truncated` when a list was cut at the server's limit. */
export type AuditFacets = { actors: string[]; entity_types: string[]; actions: string[]; truncated: boolean }
export const getAuditFacets = () => apiGet<AuditFacets>('/api/v1/audit/facets')

/** Everything recorded about one entity, oldest first. 404 when nothing was. */
export type AuditTrace = { entity_id: string; entry_count: number; entries: AuditLogEntry[] }
export const getAuditTrace = (entityId: string) =>
  apiGet<AuditTrace>(`/api/v1/audit/trades/${encodeURIComponent(entityId)}/trace`)

/** What one actor has done: counts per action and when they were first and last seen. 404 when nothing. */
export type AuditActorSummary = {
  actor: string
  action_count: number
  first_seen: string | null
  last_seen: string | null
  actions: Record<string, number>
}
export const getAuditActorSummary = (actor: string) =>
  apiGet<AuditActorSummary>(`/api/v1/audit/actors/${encodeURIComponent(actor)}/summary`)

export const verifyAuditChain = () => apiPost<AuditChainVerifyResult>('/api/v1/audit/verify')

// A file the server only hands to a signed-in user. A plain <a href> cannot carry the Bearer token,
// so the browser would be given a 401 page: fetch it with the token and save the blob instead.
export async function downloadAuthenticated(path: string, fallbackFilename: string): Promise<void> {
  const token = getAccessToken()
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
    credentials: 'include',
  })
  if (!res.ok) {
    let detail = 'Download failed'
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (typeof body.detail === 'string') detail = body.detail
    } catch {
      // not JSON: keep the generic message
    }
    throw new ApiError(res.status, detail)
  }
  const named = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') ?? '')
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = named?.[1] ?? fallbackFilename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/** The log as a file; with a query, only the entries it matches (what the page is showing). */
export const downloadAuditExport = (format: 'csv' | 'ndjson', query: AuditQuery = {}) => {
  const params = auditQueryString(query)
  params.set('format', format)
  return downloadAuthenticated(`/api/v1/audit/export?${params.toString()}`, `audit_log_export.${format === 'ndjson' ? 'ndjson' : 'csv'}`)
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
  strategy_name: string | null
  /** What the price rests on: real, synthetic, mixed, supplied or unknown. */
  data_source: string
  /** A live trade's order; a paper fill has none. */
  order_id: string | null
  /** What a paper fill cost; a live trade's costs are not recorded. */
  costs: number | null
  /** A paper close done by hand without the risk gate. */
  administrative: boolean
}

/** What narrows the execution ledger; each field is optional and they combine. */
export type OrdersQuery = {
  mode?: 'paper' | 'live' | 'both'
  status?: string
  symbol?: string
  strategyId?: string
  limit?: number
  offset?: number
}

export const listOrders = (query: OrdersQuery = {}) => {
  const params = new URLSearchParams({ mode: query.mode ?? 'both' })
  if (query.status) params.set('status', query.status)
  if (query.symbol) params.set('symbol', query.symbol)
  if (query.strategyId) params.set('strategy_id', query.strategyId)
  params.set('limit', String(query.limit ?? 100))
  params.set('offset', String(query.offset ?? 0))
  return apiGetPage<UnifiedExecution>(`/api/v1/orders?${params.toString()}`)
}

export type PositionByStrategy = {
  mode: 'paper' | 'live'
  strategy_id: string
  strategy_name: string
  symbol: string
  quantity: number
  avg_cost: number
  realized_pnl: number
  // false: an open position on an inactive subscription, which nothing is watching
  managed: boolean
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
export const acceptPortfolioRecommendation = (id: string, notes?: string) =>
  apiPost<PortfolioRecommendation>(`/api/v1/portfolio/recommendations/${id}/accept`, { notes })
export const rejectPortfolioRecommendation = (id: string, notes?: string) =>
  apiPost<PortfolioRecommendation>(`/api/v1/portfolio/recommendations/${id}/reject`, { notes })

export type PortfolioSummary = {
  as_of: string
  active_strategy_count: number
  paper_realized_pnl_today: number
  // the costs inside that figure's fills: the realized P&L above is before them
  paper_costs_today: number
  paper_open_position_count: number
  paper_unmanaged_position_count: number
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
export const putGatewayConfig = (rawText: string, versionId: number) =>
  apiPut<ApplyResult>('/api/v1/gateway/config', { raw_text: rawText }, onConfigVersion(versionId))
// Replaces the LLM fallback order and nothing else in the config
export const putLlmProviderOrder = (order: string[], versionId: number) =>
  apiPut<ApplyResult>('/api/v1/gateway/llm-provider-order', { order }, onConfigVersion(versionId))
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
  // What the broker itself last said about these credentials -- which the clock-derived
  // `token_status` cannot know. Set while its quote feed is being refused (HTTP 401/403);
  // null means "no rejection known", not "accepted".
  feed_rejection_status?: number | null
  feed_rejection_detail?: string | null
  feed_rejected_at?: string | null
  // Quotes this broker answered that were not acted on because the exchange's own stamp on them was
  // from a previous day (or too old), since the credentials last changed.
  quotes_refused_count?: number
  quotes_refused_last_symbol?: string | null
  quotes_refused_last_reason?: string | null
  quotes_refused_last_at?: string | null
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
export type AlertLevel = 'kill-switch' | 'sign-off' | 'go-live' | 'daily' | 'engine'
export const NOTIFICATION_CHANNELS: NotificationChannelName[] = ['telegram', 'discord', 'slack']
export const ALERT_LEVELS: AlertLevel[] = ['kill-switch', 'sign-off', 'go-live', 'daily', 'engine']
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
// Non-secret settings only (no token can travel here): what the alert-level toggles use.
export type UpdateNotificationChannelInput = {
  enabled?: boolean
  alert_levels?: string[]
  allowed_sender_ids?: string[]
}
export const updateNotificationChannel = (channel: string, body: UpdateNotificationChannelInput) =>
  apiPatch<void>(`/api/v1/notification-channels/${channel}`, body)
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

// ---- Engine alerts: what the trading engine needed a person for ----
export type EngineAlertSource = 'watcher' | 'event' | 'alertmanager'
export type EngineAlertSeverity = 'critical' | 'warning' | 'info'
export type EngineAlert = {
  id: string
  key: string
  source: EngineAlertSource
  kind: string
  severity: EngineAlertSeverity
  title: string
  detail: string
  subscription_id: string | null
  opened_at: string
  last_seen_at: string
  /** Null while it is still happening. */
  resolved_at: string | null
  /** When the opening notice went out; null if it cleared before it had lasted long enough to announce. */
  notified_at: string | null
}
export type EngineAlertList = {
  /** Across everything, whatever `limit` cut the list to. */
  open_count: number
  critical_open_count: number
  alerts: EngineAlert[]
}
export const listEngineAlerts = (state: 'open' | 'resolved' | 'all' = 'all', limit = 100) =>
  apiGet<EngineAlertList>(`/api/v1/engine-alerts?state=${state}&limit=${limit}`)
export type AlertmanagerStatus = {
  /** False when this deployment has no Alertmanager. */
  configured: boolean
  /** Null when it was not asked. */
  reachable: boolean | null
  detail: string
  token_ready: boolean
  last_alert_received_at: string | null
}
export const getAlertmanagerStatus = () => apiGet<AlertmanagerStatus>('/api/v1/engine-alerts/alertmanager')

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
  rejected_by: string | null
  rejected_at: string | null
  rejection_reason: string | null
  // The same people by e-mail, where the id is a known user.
  staged_by_label: string | null
  confirmed_by_label: string | null
  applied_by_label: string | null
  rejected_by_label: string | null
}
// The limits that can be staged, with what is in force.
export type RiskLimitCatalogEntry = {
  name: string
  label: string
  description: string
  unit: string
  default: number
  minimum: number
  maximum: number
  // false: nothing reads this limit yet, so changing it changes nothing
  enforced: boolean
  effective: number
  applied: boolean
}
export const listRiskLimitCatalog = () => apiGet<RiskLimitCatalogEntry[]>('/api/v1/risk-limits/catalog')
export const rejectRiskLimitChange = (changeId: string, reason?: string) =>
  apiPost<RiskLimitChange>(`/api/v1/risk-limits/${changeId}/reject`, { reason })
// The key that signs every session. Never returned -- only where it came from
// and whether it can be trusted (a deployment started with defaults used to be
// seeded with a public placeholder).
export type JwtSigningKeyStatus = {
  created_at: string
  created_by: string
  weak: boolean
  weakness: string | null
}
export const getJwtSigningKeyStatus = () => apiGet<JwtSigningKeyStatus>('/api/v1/system/jwt-signing-key')
export const rotateJwtSigningKey = () =>
  apiPost<{ rotated_at: string; rotated_by: string; sessions_revoked: number }>('/api/v1/system/jwt-signing-key/rotate')
// Backups of the database (Postgres dumps) and of the data lake. Listing reads the folders and the
// manifests only; the run that makes each backup also checks it.
export type BackupEntry = {
  backup_id: string
  // incomplete: a dump with no manifest, a manifest that cannot be read -- listed so it is seen, never to restore from
  status: 'ok' | 'incomplete'
  created_at: string
  age_seconds: number
  size_bytes: number
  sha256: string | null
  note: string | null
  // Postgres only
  alembic_revision: string | null
  audit_tip_sequence: number | null
  audit_tip_hash: string | null
  table_count: number | null
  // Lake only
  files: number | null
  // Rows held: every table's (Postgres) or every Parquet file's (lake)
  total_rows: number | null
}
export type BackupKind = {
  kind: 'postgres' | 'lake'
  directory: string
  // Newest first, at most ten
  backups: BackupEntry[]
  // Age of the newest usable backup; null when there is none
  newest_ok_age_seconds: number | null
  // No usable backup, or the newest is older than stale_after_hours
  stale: boolean
}
export type BackupsResponse = {
  stale_after_hours: number
  keep_count: number
  retention_days: number
  schedule: string
  postgres: BackupKind
  lake: BackupKind
}
export type PostgresBackupRun = { backup: BackupEntry; pruned: string[] }
export const getBackups = () => apiGet<BackupsResponse>('/api/v1/system/backups')
// Takes as long as the dump does (seconds to minutes); the nightly job does exactly this.
export const runPostgresBackup = () => apiPost<PostgresBackupRun>('/api/v1/system/postgres-backup')
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
  // What the series was computed with (the SMA, EMA and RSI carry theirs above)
  macd_fast: number
  macd_slow: number
  macd_signal_period: number
  bollinger_window: number
  bollinger_std: number
  lookback_days: number
}
export const getIndicators = (symbol: string, params: IndicatorParams = DEFAULT_PARAMS) =>
  apiGet<IndicatorSeries>(`/api/v1/market-data/indicators/${encodeURIComponent(symbol)}?${indicatorQuery(params)}`)

// ---- The trading holidays the calendar knows for a year ----
export type HolidayEntry = {
  date: string
  weekday: string
  name: string | null
  source: string
  // A holiday that falls on a Saturday or Sunday changes nothing: the exchange is shut anyway
  on_weekend: boolean
}
export type Holidays = {
  year: number
  holidays: HolidayEntry[]
  // False: the calendar file lists nothing for this year, so its movable holidays are being treated as trading days
  calendar_file_covers_year: boolean
  warning: string | null
}
export const getHolidays = (year?: number) =>
  apiGet<Holidays>(`/api/v1/market-data/holidays${year === undefined ? '' : `?year=${year}`}`)

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
  // The day a backtest requires daily data for, and how many trading days the newest daily data is short of it
  // (0 is current; null when the lake holds no daily data)
  required_daily_data_date: string
  daily_data_behind_trading_days: number | null
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
  is_active: boolean
  last_synced_at: string
}
export type InstrumentQuery = {
  /** Any part of the symbol, in any case */
  q?: string
  instrumentType?: string
  underlying?: string
  limit?: number
  offset?: number
}
/** One page of the instrument master by symbol, and how many match in all. */
export const listInstruments = (query: InstrumentQuery = {}) => {
  const params = new URLSearchParams()
  if (query.q) params.set('q', query.q)
  if (query.instrumentType) params.set('instrument_type', query.instrumentType)
  if (query.underlying) params.set('underlying', query.underlying)
  if (query.limit !== undefined) params.set('limit', String(query.limit))
  if (query.offset) params.set('offset', String(query.offset))
  const suffix = params.toString()
  return apiGetPage<Instrument>(`/api/v1/market-data/instruments${suffix ? `?${suffix}` : ''}`)
}
/** The underlyings that have options in the master, by name. */
export const getOptionUnderlyings = () => apiGet<string[]>('/api/v1/market-data/option-underlyings')

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
export const downloadChatExport = (sessionId: string, format: 'markdown' | 'json' = 'markdown') =>
  downloadAuthenticated(
    `/api/v1/chat/sessions/${sessionId}/export?format=${format}`,
    `chat_${sessionId}.${format === 'json' ? 'json' : 'md'}`
  )

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
    credentials: 'include',
  })

  if (res.status === 401 && !_isRetry && token) {
    const refreshed = await refreshSession()
    if (refreshed.kind === 'ok') return streamChatMessage(sessionId, content, onChunk, true)
    if (refreshed.kind === 'transient') {
      throw new ApiError(refreshed.status, 'Session refresh is temporarily unavailable')
    }
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
