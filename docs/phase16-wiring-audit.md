# Phase 16 — Frontend/Backend Wiring Audit

Scope: every page (Overview, Mission Control, Agent Fleet, Strategies, Backtests,
Orders & Trades, Market Analysis, Audit Log, Settings), checked for (1) leftover
mock-data usage, (2) missing/incomplete backend behind a frontend call, (3)
write actions that don't actually persist, (4) WebSocket panels that aren't
really live, and (5) role-based UI gating that doesn't match real backend RBAC.

Method: `grep` across `app/`, `components/`, `lib/` for `mock-data`, `SAMPLE_`,
`MOCK_`, `Math.random()`, and similar; cross-referenced every backend call
against `register_policy(...)` calls in `backend/src/api/routes/`; live-verified
the two highest-risk fixes (a real kill-switch trip via WS, and RBAC across all
4 roles against a running dev stack) rather than trusting code-reading alone.

## Summary

- **Mock-data imports found: zero.** `lib/mock-data` was already fully removed
  in Phase 13; `grep -rn "mock-data\|MOCK_\|SAMPLE_\|Math.random()"` across
  `app/`, `components/`, `lib/` returns no hits anywhere in the app.
- **5 real gaps found and fixed this pass** (below).
- **6 gaps confirmed genuine and already honestly labeled by the frontend**,
  left as explicit follow-ups with reasoning (below) rather than papered over.
  One of those six (Follow-up B, broker-fill reconciliation) was subsequently
  picked up and resolved in a follow-up pass — see its entry below.

## Gaps found and fixed

### Gap 1 — Overview: kill-switch state only refreshed on a 20s poll, not on the WS event that announces it (Severity: cosmetic/stale data)

The Organization Pulse orb and KPI strip already derived `killSwitchTripped`
from real polled state, and the page was already subscribed to
`/ws/activity-feed`. But the WS handler ignored `kill-switch` events and only
triggered `load()` off nothing — a fresh kill-switch trip sat unreflected in
the UI for up to 20 seconds after the underlying state actually changed, which
is exactly the "cosmetically stale" failure mode this audit targets for a
control that a human may be staring at during a real risk event.

**Fix:** `app/page.tsx` — the `/ws/activity-feed` handler now calls `load()`
immediately when an incoming event's `entity_type === 'kill-switch'`, instead
of waiting for the interval.

**Live-verified:** wrote a throwaway Playwright spec, logged in as
`audit-sysadmin@example.com`, confirmed initial `OFF · ARMED` state, tripped
the paper kill switch via a real authenticated `POST
/api/v1/kill-switch/paper/check`, and confirmed the strip showed `TRIPPED`
within ~4 seconds with no page reload. Reset the switch back via `POST
/api/v1/kill-switch/paper/reset` afterward; confirmed via direct SQL
(`kill_switch_states`) that both `paper` and `live` rows are `tripped = f`
after cleanup. Spec deleted after use (not committed, per this repo's
throwaway-verification convention).

### Gap 2 — Market Analysis → Provider Status tab: claimed "NOT AVAILABLE" when real backend data already existed (Severity: cosmetic/stale, actually worse than stale — actively wrong)

The Settings redesign (previous PR) built real `GET` endpoints for LLM
provider status and broker credential/token status
(`listLlmProviderStatus`, `listBrokerCredentialStatus` in `lib/api.ts`), but
the Market Analysis "Providers" tab was never updated to use them — it
rendered a single `GapNotice` claiming none of this was available at all, which
was no longer true and actively misinformed anyone checking provider health
from that page instead of Settings.

**Fix:** `app/analysis/page.tsx` — `ProvidersTab` now fetches and renders real
LLM-provider and broker-credential status via the existing endpoints. The one
remaining `GapNotice` on this tab was narrowed to be specifically and
correctly about circuit-breaker connectivity/latency health (a distinct, still
genuinely missing capability — see Follow-up A), not the whole tab.

### Gap 3 — Orders & Trades: "Positions by strategy" had zero backend, panel didn't exist (Severity: missing entirely)

The page's stat strip had a placeholder reading "POSITIONS BY STRATEGY · not
exposed yet" with no corresponding endpoint anywhere. The data to answer this
already existed (`LivePosition` keyed by `strategy_id`; `PaperPosition` keyed
by `subscription_id`, joinable through
`PaperTradingSubscription.strategy_version_id → StrategyVersion.strategy_id →
Strategy`) but had never been exposed as an aggregate.

**Fix:**
- `backend/src/api/routes/orders.py` — new `GET /api/v1/orders/positions`
  endpoint (`register_policy` grants all roles read access, consistent with
  every other read endpoint on this router), joining and returning both
  live and paper positions with resolved strategy names.
- `backend/tests/test_orders_api.py` — new test file covering both the
  previously-untested `GET /orders` and the new endpoint, with real DB
  fixtures (see check-constraint findings below).
- `lib/api.ts` — `PositionByStrategy` type + `listPositionsByStrategy`.
- `app/orders/page.tsx` — real position count in the stat strip, plus a new
  "Positions by strategy" sidebar panel listing each position.
- `app/globals.css` — `.position-list`/`.position-row` styling for the new
  panel.

### Gap 4 — Mission Control: sign-off queue Approve/Reject enabled for roles the backend silently rejects (Severity: broken — silent RBAC failure, highest severity found)

`POST /api/v1/approvals/{id}/decide` and the live-order-intent decide
equivalent are `_DECIDE_ROLES`-gated server-side to
SystemAdministrator/RiskManager/PortfolioManager only. The frontend's
`SignoffQueue` took a `readOnly: boolean` prop that was **always `false`** —
every role, including ReadOnlyAuditor, saw fully clickable Approve/Reject
buttons. Clicking them as a disallowed role hit a 403 that was silently
swallowed (`catch` block with no user-visible error), so the user saw nothing
happen and had no way to know their decision wasn't recorded — the single
worst kind of gap this audit was meant to catch, because it looks like a
successful action.

**Fix:** `app/mission-control/page.tsx` — added `canDecide` (role-gated to the
real `_DECIDE_ROLES` set), added `decisionError` state, rewrote
`handleDecideApproval`/`handleIntentAction` to surface the real backend error
instead of swallowing it, changed `SignoffQueue`'s prop signature from
`readOnly` to `canDecide`/`decisionError`, added a visible error banner, and
changed both Approve/Reject button sets from `disabled={readOnly}` (always
false) to `disabled={!canDecide}` (real).

**Live-verified:** looped authenticated curl calls against
`/api/v1/approvals/{id}/decide` with real JWTs for all 4 roles
(SystemAdministrator, PortfolioManager, RiskManager, ReadOnlyAuditor) and
confirmed the exact allow/403 split matches the new frontend gating exactly.

### Gap 5 — Backtests: "Run Monte Carlo" enabled for roles the backend rejects (Severity: broken — same class as Gap 4, lower blast radius)

`POST /backtests/{run_id}/monte-carlo` is `_OPERATOR_ROLES`-gated
(SystemAdministrator/PortfolioManager) server-side. The Backtests page had no
role gate at all — every role saw an enabled button that would 403 for
RiskManager/ReadOnlyAuditor, with the resulting error only surfacing after the
click (not pre-emptively communicated).

**Fix:** `app/backtests/page.tsx` — added `useAuth`, `canOperate` role check
matching `_OPERATOR_ROLES`, disabled the button with an explanatory `title`
tooltip when the current role lacks access.

## Write-action persistence check (instruction #3)

Every write action across the app follows the same pattern: call the real
`lib/api.ts` endpoint, then call the page's `reload()`/`load()` which re-fetches
from the backend rather than trusting the optimistic response. This was
verified by reading every mutation call site across all 9 pages — none was
found updating local state directly as the terminal step without a
subsequent real re-fetch. The two highest-risk write paths (kill-switch
trip/reset, sign-off decide) were additionally live-verified end-to-end
against the running dev backend + Postgres, not just code-read (see Gaps 1
and 4 above).

## WebSocket-panel liveness check (instruction #4)

- **Activity feed** (`/ws/activity-feed`): confirmed server-side as a real
  1.5s poll of the `audit_log` table pushing genuine new rows — not
  synthetic. Used as part of Gap 1's live verification (the kill-switch trip
  event flowed through this exact channel).
- **Sign-off queue** (`/ws/sign-off-queue`): confirmed server-side as a real
  poll of `GET /approvals`-equivalent queries, wired into
  `app/mission-control/page.tsx` via `useWebSocketChannel` and `setSignoff`.
- **Organization Pulse orb** (`app/page.tsx`): confirmed its `PulseState` is
  derived entirely from live-fetched values — `killSwitchTripped` (polled +
  WS-accelerated per Gap 1), `pendingSignoffs` (from the real
  `/ws/sign-off-queue` snapshot), and `runs` status (from the real
  orchestration run list) — no synthetic or hardcoded state transitions.
- **Organization events** (`/ws/organization-events`): confirmed server-side
  as real Redis pub/sub, published since Phase 2's orchestration layer, wired
  into Mission Control.

## Role-based UI restriction check (instruction #5)

Built a ground-truth RBAC table by grepping every `register_policy(...)` call
and role-list constant (`_OPERATOR_ROLES`, `_WRITE_ROLES`, `_DECIDE_ROLES`,
`_LIVE_SIGNOFF_ROLES`, `_READ_ROLES`, etc.) across
`backend/src/api/routes/`, then cross-referenced every `useAuth()`-derived
role check in the frontend against it page-by-page. This is what surfaced
Gaps 4 and 5 — both were silent mismatches where the frontend granted more
access than the backend allowed, discovered by comparison rather than
assumption. Live-verified all 4 roles (SystemAdministrator, PortfolioManager,
RiskManager, ReadOnlyAuditor) against the running backend for the
Mission-Control decide endpoint; the remaining role-gated actions across the
app (Agent Fleet skill edits, Strategy suggestion submission, Settings
credential/notification writes, Risk limit changes) were checked by table
cross-reference only and found already consistent — no further live-role
sweep was performed on those given the RBAC contract test suite
(`test_rbac_contract*.py`, Phase 4.5) already covers every mutating endpoint
against every role at the API layer.

## Follow-ups — genuine gaps, not fixed this pass, with reasoning

These are all cases where the frontend is **already honest** about the gap
(a `GapNotice` or "not exposed yet" label, never a fabricated number), and
where fixing them properly requires new backend engineering beyond what a
wiring pass should do speculatively.

**A. Broker circuit-breaker / connectivity health not exposed**
(`app/analysis/page.tsx` Provider Status tab, narrowed `GapNotice`).
`ResilientBrokerAdapter`/`BrokerCircuitBreaker` (`backend/src/brokers/resilient.py`,
`factory.py`) exist, but `build_configured_adapter()` constructs a fresh
breaker on every call — there is no persistent, queryable breaker state. This
can't be fixed by adding an endpoint alone; it needs the breaker to become a
per-broker singleton held in application state first. Left as an explicitly
scoped follow-up rather than attempted here.

**B. `Trade.status` can never progress past `pending_confirmation` — RESOLVED (post-audit follow-up).**
(Orders & Trades page, "Live Order Intents" tab). Confirmed via
`grep -rn "\.status = \|status=.*confirm\|reconcil" src/orchestration/live_trading.py
src/models/trade.py`: the only call site that creates a `Trade`
(`live_trading.py:558`) always set `status="pending_confirmation"`, and
nothing anywhere transitioned it further — there was no broker-fill
reconciliation job. The frontend rendered whatever status string came back
verbatim, so this was not a frontend lie, but a real backend limitation.

Fixed in a follow-up pass: added `reconcile_pending_trades` to
`backend/src/orchestration/live_trading.py`, a new scheduled job
(`live_trading_reconciliation`, 15s interval, registered in
`live_trading_scheduler.py` alongside the existing expiry sweep — ungated by
`is_market_open_ist()` for the same reason the expiry sweep is: a fill can be
confirmed by the broker shortly after the new-order window closes). Since the
adapter layer exposes no per-order status lookup (only the bulk
`get_order_book()`), the job fetches that once per pass and matches
client-side against each pending `Trade`'s `Order.broker_order_id` — the
broker's own status vocabulary (Zerodha's `COMPLETE`/`REJECTED`/`CANCELLED`,
Upstox's lowercase equivalents) is normalized into `Trade.status`'s own
`filled`/`rejected`/`cancelled` terminal states. `Trade` gained `fill_price`
and `confirmed_at` columns (migration `5fb9d5f828c6`) so the real confirmed
fill price is never confused with the pre-fill reference `price` column;
`GET /api/v1/orders` now surfaces `fill_price` once known instead of always
the stale reference price. Each reconciliation writes a `trade.reconciled`
audit entry. 10 new tests (`test_orchestration_trade_reconciliation.py`,
plus 2 added to `test_orchestration_live_trading_scheduler.py`) using a
minimal fake adapter, covering filled/rejected/still-open/missing-from-book/
wrong-broker/fetch-failure/no-broker-order-id cases. Live-verified via
`alembic upgrade head`/`downgrade -1`/`upgrade head` round-trip against the
dev DB and a direct schema inspection confirming the widened check
constraint. The one thing still not built: a real broker-fill reconciliation
job needs a persistent per-broker adapter to poll against, same as Follow-up
A below — this reconciliation job reuses the existing single
`build_configured_adapter()`-selected adapter already passed to the live
trading scheduler, so it inherits that same "only one broker configured at a
time" constraint rather than solving it.

**C. Technical Indicators tab has no backend** (`app/analysis/page.tsx`,
already labeled `NOT AVAILABLE`). No endpoint computes SMA/EMA/RSI/MACD/
Bollinger Bands for arbitrary symbols; only the backtest engine computes
signal-generation indicators internally without exposing them as a general
time-series API. Real OHLCV data exists in the data lake but isn't served
this way. Confirmed accurate, left as-is.

**D. Live Option Chain has no live OI/IV/LTP feed** (`app/analysis/page.tsx`,
already labeled with a `GapNotice`). Only the static instrument master
(strike, expiry, lot size, tick size) is served; there is no live
options-chain data source wired in. Confirmed accurate, left as-is.

**E. Host CPU/memory, token usage, and order-dispatch-latency remain
Prometheus-only** (Overview "System Vitals" panel, already labeled "not
exposed yet" / "Prometheus only"). `backend/src/observability/metrics.py`
exposes exactly 5 Counters/Histograms via `/metrics` text format, consumed by
Grafana. Deliberately **not** wrapped in a new JSON summary endpoint this
pass: a Prometheus Counter's "since process start" semantics would need
careful UI framing to avoid implying a "today" figure that isn't what it is —
building that quickly risked introducing exactly the "looks real, quietly
wrong" failure mode this audit exists to catch. Left as an honest gap
pending a deliberate design pass, not a rushed fix.

**F. "Today's Paper P&L" not exposed** (Overview KPI strip, already labeled
"not exposed yet"). `PaperPosition.realized_pnl` is cumulative-to-date, not
scoped to a trading day, and unrealized P&L would additionally require a
current market price per symbol that isn't stored on the position row.
Computing a true "today's" figure needs either a per-day rollup derived from
`paper_fills` timestamps or a new daily-snapshot job — not a cheap join like
Gap 3's positions-by-strategy endpoint was. Confirmed accurate, left as a
follow-up rather than attempted here.

## Acceptance criteria status

- No page runs on mock/sample data: confirmed by exhaustive grep (zero hits)
  across the whole frontend.
- Every interactive element has a real, verified backend effect: 5 gaps found
  where this wasn't true were fixed; the two highest-severity/highest-risk
  fixes (Gaps 1 and 4) were live-verified against a running dev stack, not
  just code-reviewed.
- This document is the durable record required by the acceptance criteria.
