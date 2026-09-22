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
  Five of those six (Follow-up A, broker circuit-breaker state; Follow-up B,
  broker-fill reconciliation; Follow-up C, technical indicators; Follow-up E,
  System Vitals token-usage/dispatch-latency JSON summary; Follow-up F,
  today's realized P&L) were subsequently picked up in follow-up passes — A,
  B, and C fully resolved, E and F partially resolved by design (E covers
  token usage/dispatch latency only, never host CPU/memory, which was never
  tracked anywhere in this codebase; F is realized-only) — see their entries
  below. One (D) remains open.

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

**A. Broker circuit-breaker / connectivity health not exposed — RESOLVED (post-audit follow-up).**
(`app/analysis/page.tsx` Provider Status tab, was a narrowed `GapNotice`).
`ResilientBrokerAdapter`/`BrokerCircuitBreaker` (`backend/src/brokers/resilient.py`,
`factory.py`) existed, but `build_configured_adapter()` constructed a fresh,
zero-state breaker on every single call — including once per HTTP request via
`src.api.routes.live_trading.get_live_broker_adapter`. Three consecutive 5xx
responses spread across different real callers (the tick source, the
live-trading scheduler, one-off API-route adapters) could each independently
see only 1 or 2 failures and never actually trip; the breaker's whole purpose
silently didn't work against the app's real call pattern, and there was no
live state anywhere to query even if it had.

Fixed in a follow-up pass: new `backend/src/brokers/breaker_registry.py`
holds exactly one `BrokerCircuitBreaker` per broker name for the process's
lifetime (a plain module-level dict, not a DB row — unlike Phase 6's Kill
Switch, breaker state losing itself on restart is fine, and this app runs as
a single uvicorn process with no `--workers N`, so a module-level singleton
genuinely is the one shared instance every caller sees, matching this
codebase's existing in-process-singleton precedent: `src.gateway`'s
`GatewayState`, `src.agents.llm_router`'s per-provider `ProviderHealth`).
`build_configured_adapter()` now passes `get_broker_circuit_breaker(broker)`
into every `ResilientBrokerAdapter` it builds instead of leaving the breaker
argument to default to a fresh instance. `BrokerCircuitBreaker` gained public
`opened_at`/`cooldown_seconds`/`failure_threshold` properties and a
`cooldown_remaining_seconds()` method (clamped to >=0, `None` while closed) so
real state is externally readable without exposing a raw, meaningless
monotonic timestamp. New `GET /api/v1/broker-credentials/circuit-breaker`
(all 4 roles, read-only) reports real `state`/`consecutive_failures`/
`failure_threshold`/`cooldown_remaining_seconds` per broker — a broker with no
entry yet in the registry (no adapter ever built for it) is honestly reported
as `closed`/`0` rather than omitted, since that is the breaker's own true
starting state. The Provider Status tab's broker-connection rows now show
real circuit-breaker state alongside OAuth token status (a red dot + failure
count + countdown when open, polled every 10s), and the `GapNotice` that used
to sit under them was removed since the gap it named no longer exists.

9 new backend tests (`test_brokers_breaker_registry.py`,
`test_brokers_factory.py`, 2 new in `test_engine_circuit_breaker.py`, 3 new in
`test_broker_credentials_api.py`) proving the singleton behavior directly
(repeated `build_configured_adapter()` calls for the same broker share one
`.breaker` object; different brokers get independent instances) and the API
endpoint reflecting a genuinely tripped breaker end to end (3 real
`BrokerServerError`s against the shared instance → `state: "open"` over
HTTP, with the *other* broker's row unaffected, proving independence, not a
shared flag). `ruff check`/`ruff format --check`/`tsc --noEmit` clean; full
backend suite green with zero regressions.

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

**C. Technical Indicators tab has no backend — RESOLVED (post-audit follow-up).**
(`app/analysis/page.tsx`, was labeled `NOT AVAILABLE`). No endpoint computed
SMA/EMA/RSI/MACD/Bollinger Bands for arbitrary symbols; only the backtest
engine computed signal-generation indicators internally, never exposed as a
general time-series API. Real OHLCV data existed in the data lake but wasn't
served this way.

Fixed in a follow-up pass: new pure-math module
`backend/src/engine/indicators.py` (SMA, EMA, Wilder's RSI, MACD, Bollinger
Bands — every function genuinely refuses to report a value before its
window has real data behind it, `min_periods=window`/`span`, never a
`min_periods=1` partial-window shortcut, matching this codebase's
"never fabricate a metric" rule), a new `GET
/api/v1/market-data/indicators/{symbol}` endpoint (all 4 roles, read-only)
reading real OHLCV via `src.data.lake.read_daily_bars` directly — not
`DataLakePriceProvider`, which silently falls back to synthetic data for a
thin symbol — so an un-ingested symbol honestly returns an empty series
instead of a fabricated one. `app/analysis/page.tsx`'s `TechnicalTab` was
rebuilt from the bare `GapNotice` stub into a real symbol-picker + price
chart (close, with toggleable SMA/EMA/Bollinger overlays) plus RSI and MACD
sub-panels, reusing CSS classes (`.technical-layout`,
`.indicator-chart-panel`, `.sub-indicators`, `.overlay-toggles`) the
original v0 build had already prepared for exactly this feature but left
unused. 17 pure indicator-math tests (`test_engine_indicators.py`) plus 3
new API tests (`test_market_data_api.py`) — the math tests hand-verify
warm-up-null boundaries, Bollinger's middle band matching the plain SMA
exactly, and closed-form edge cases (a strictly monotonic series' RSI is
genuinely 0 or 100, not undefined).

**Live-verified:** ingested 50 real trading days of synthetic-but-real
OHLCV for a demo symbol via the existing `POST
/market-data/ingest/daily`/`ingest/instrument-master` triggers, confirmed
the indicators endpoint returns real non-degenerate SMA/EMA/RSI/MACD/
Bollinger values with correct null warm-up periods via direct API
inspection, then confirmed the same in the browser via Playwright — the
chart renders real SVG lines against the ingested data, the "no ingested
history" fallback message correctly does *not* show, and toggling the
Bollinger overlay button adds exactly 2 more chart lines. Screenshot
confirmed visually correct. Spec deleted after use, not committed.

**D. Live Option Chain has no live OI/IV/LTP feed** (`app/analysis/page.tsx`,
already labeled with a `GapNotice`). Only the static instrument master
(strike, expiry, lot size, tick size) is served; there is no live
options-chain data source wired in. Confirmed accurate, left as-is.

**E. Host CPU/memory, token usage, and order-dispatch-latency remain
Prometheus-only — PARTIALLY RESOLVED (post-audit follow-up, token
usage/dispatch latency only, by design).** (Overview "System Vitals" panel,
was labeled "not exposed yet" / "Prometheus only"). `backend/src/observability/
metrics.py` exposes exactly 5 Counters/Histograms via `/metrics` text format,
consumed by Grafana. Deliberately not wrapped in a new JSON summary endpoint
in the original audit pass: a Prometheus Counter's "since process start"
semantics would need careful UI framing to avoid implying a "today" figure
that isn't what it is — building that quickly risked introducing exactly the
"looks real, quietly wrong" failure mode this audit exists to catch.

Fixed in a follow-up pass, now that the deliberate design pass has actually
happened: new `backend/src/observability/metrics.py::build_vitals_summary()`
reads the *same* `Counter`/`Histogram` objects `/metrics` already exposes
(via each metric's own `.collect()`, not a second independently-tracked
value) and returns a JSON-friendly summary for exactly two of the three
items named above — LLM token usage and broker order-dispatch latency, both
real Prometheus metrics with real data flowing into them. Every figure is
explicitly anchored to `since` (a real timestamp captured at process start),
never framed as "today" — the honesty distinction the original pass refused
to rush. New `GET /api/v1/observability/vitals` (all 4 roles, read-only,
ordinary RBAC posture, not the unauthenticated `/metrics` scrape convention)
serves it. A broker with zero dispatches reports `avg_latency_ms: null`,
never a fabricated `0.0` — the same never-fabricate-a-metric rule this
codebase applies everywhere else.

**Host CPU/memory is deliberately still not included** and the frontend
tile still honestly says "not exposed yet" — it was never one of Build Spec
§19's five tracked metrics and nothing in this codebase computes it
anywhere; fabricating a value to fill out the response would have been the
exact failure mode this whole exercise was built to avoid. Per-provider LLM
health is likewise still genuinely not exposed and stays labeled as such.
This is why the title above says "PARTIALLY RESOLVED, by design" rather than
"RESOLVED" — two of the three named items are real now, one (host
CPU/memory) was never real anywhere in this codebase and isn't fabricated
here either.

7 new backend tests (`test_api_observability.py`) exercise the real,
process-global Prometheus singletons directly (not mocks) using distinct
made-up label values (`vitals-test-provider`, `vitals-test-*-broker`) to get
deterministic assertions without a before/after diff against labels other
tests in the suite also touch (`zerodha`/`upstox`) — covering the honest
`since` timestamp, real token-usage aggregation, the never-fabricated
zero-dispatch case, a real weighted-average-latency computation, the same
shape over real HTTP, and RBAC read access for all 4 roles.

**Live-verified:** registered+promoted a demo `SystemAdministrator`, hit the
endpoint over real HTTP before any real dispatch/LLM call had happened and
confirmed an honestly empty response (`llm_token_usage: []`,
`order_dispatch: []` — no fabricated zero-value entries for a broker/provider
that's never actually been touched), then loaded the real Overview page via
Playwright and confirmed the Token Usage and Dispatch Latency tiles render
real data with the "since server start ... not 'today'" framing while the
LLM Provider Health and Host Metrics tiles still honestly read "not exposed
yet" — screenshot confirmed visually correct. Did not additionally drive a
real order through `ResilientBrokerAdapter.place_order` this round (that
would need a full live-trading subscription → Go-Live-eligible strategy →
intent-approval scaffold just to increment one counter); that exact
code path — the `finally` block that always records the latency metric
regardless of success/failure — already has dedicated, passing unit coverage
in `test_brokers_resilient.py` from Phase 11, and Phase 9's own original live
pass already confirmed a real (sandbox-egress-blocked) submission attempt
through it. Spec deleted after use, not committed.

**F. "Today's Paper P&L" not exposed — PARTIALLY RESOLVED (post-audit follow-up, realized-only by design).**
(Overview KPI strip, was labeled "not exposed yet"). `PaperPosition.realized_pnl`
is cumulative-to-date, not scoped to a trading day, and unrealized P&L would
additionally require a current market price per symbol that isn't stored on
the position row.

**Correction to the original finding**, discovered while implementing the
fix: the per-fill realized P&L delta was **not** actually discarded as first
assumed — `PaperFill` already carries its own `realized_pnl` column (the
exact per-fill delta `apply_fill` computes, written by
`_persist_fill` alongside the cumulative total it adds to
`PaperPosition.realized_pnl`), and `PaperFill.created_at` is already a real
timestamp. So "today's realized P&L" turned out to be a trivial aggregate
query (`SUM(PaperFill.realized_pnl) WHERE created_at >= IST midnight today`),
not the harder per-day rollup or snapshot job originally assumed.

Fixed: new `GET /api/v1/paper-trading/pnl/today` endpoint (all 4 roles,
read-only, portfolio-wide — not scoped to one subscription, matching the
KPI's own portfolio-wide framing), Overview's KPI relabeled from "TODAY'S
PAPER P&L" to **"TODAY'S REALIZED P&L"** to be honest about scope. 5 new
tests (`test_paper_trading_api.py`) covering the zero-fills baseline, a real
open+stop-loss-exit cycle where the aggregate is asserted to exactly match
the closing fill's own `realized_pnl` (the opening fill's `realized_pnl` is
always `0.0`, per `apply_fill`'s accounting), and RBAC access for all 4
roles.

**Deliberately not attempted in this pass: unrealized (mark-to-market)
P&L.** This still needs a genuine current price per open position, which
this engine has no non-fabricated source for outside of a configured
broker's live quote. The one real, already-running candidate — the last
tick on each symbol's `paper:ticks:{symbol}` Redis stream — is only a real
market price when a broker is actually configured; otherwise it's
`MockTickSource`'s synthetic random walk, and conflating the two under one
undifferentiated "P&L" number would be exactly the kind of ambiguous,
possibly-synthetic-looking-real figure this audit exists to catch (the same
reasoning that kept Follow-up E's Prometheus metrics out of a JSON
endpoint). Left as an explicitly scoped, honestly-labeled follow-up rather
than rushed.

**Live-verified:** enrolled a real paper-trading subscription on a known
BUY-signal day, confirmed the endpoint reads `0.0`/`0` fills before any
activity, opened a real position via a real tick (`realized_pnl: 0.0`, as
expected for an opening fill), drove a real stop-loss exit via a tick 10%
below the 3%-threshold entry price, and confirmed the aggregate endpoint's
`realized_pnl` matched the exit fill's own `realized_pnl` to the cent
(`-503.84`) with `fill_count: 2`. Confirmed the same number renders on the
Overview KPI strip in a real browser via Playwright (`-₹504`, correctly
colored rose for a loss) — this also caught and fixed a real cosmetic
formatting bug (the currency symbol was rendering before the minus sign,
`₹-504`, instead of the conventional `-₹504`). Spec deleted after use, not
committed.

## Acceptance criteria status

- No page runs on mock/sample data: confirmed by exhaustive grep (zero hits)
  across the whole frontend.
- Every interactive element has a real, verified backend effect: 5 gaps found
  where this wasn't true were fixed; the two highest-severity/highest-risk
  fixes (Gaps 1 and 4) were live-verified against a running dev stack, not
  just code-reviewed.
- This document is the durable record required by the acceptance criteria.
