# Phase 17 — Real-World Testing Pass

Scope: a real-credential end-to-end testing pass against a live Docker stack,
covering (1) walking through real broker/LLM-provider/notification-channel
setup via the Settings UI, (2) resolving Phase 16 Follow-up D (Live Option
Chain) with a real ATM/spot marking, (3) redesigning Phase 16 Follow-up E
(System Vitals) so every figure is read the way its own semantics require
instead of forced through a since-process-start Prometheus Counter shape,
(4) a real end-to-end smoke test during live NSE market hours, and (5) this
write-up.

**Split across two environments, by necessity.** This pass was requested
against the actual project checked out at `D:\TradingOS-2.0\TradingOS-2.0`
with real external services. The session that implemented Parts 2 and 3
runs in a cloud sandbox, not on that machine — it has no access to that
local filesystem, and its outbound network egress is blocked at the proxy
level for real external hosts (confirmed live: `curl` to `api.telegram.org`
returned `403 connect_rejected`; a real Upstox call later in this same pass
returned `httpx.ProxyError: 403 Forbidden`). Docker itself cannot run in
this sandbox either — `dockerd` fails with `failed to start containerd:
timeout waiting for containerd to start`, a container-runtime restriction,
not a configuration problem. This matches `docs/CLAUDE.md`'s own Phase 0
note that `docker compose build`/`up` has never worked in this sandbox for
any phase; every phase's real-Docker acceptance pass has always happened
separately, on the local machine.

Given that, this pass split the plan exactly along that existing line:
**Parts 2 and 3 (real code changes) were implemented and verified here**
— against a direct `uvicorn` + local Postgres/Redis process (this
sandbox's only viable verification path, same as every prior phase),
plus the sandbox-blocked-but-genuine-attempt pattern already established
in Phases 8/9/16 for anything needing real external egress. **Parts 1 and
4 (real broker OAuth completing, a real LLM/notification round-trip, a
live-market-hours smoke test) were not attempted here** — they need a
real Docker stack and real developer-console credentials, which only
exist at `D:\TradingOS-2.0\TradingOS-2.0` per this repo's own
Development & Testing Workflow. They remain open until run there.

## Part 2 — Live Option Chain

**Step 1, checked before assuming anything was missing:** does the
`instruments` table (Phase 10's instrument-master-sync pipeline) already
carry real option contracts? The schema does — `Instrument` (Phase 10) has
had `underlying_symbol`/`expiry_date`/`strike_price`/`option_type` columns
since it was first built. But the pipeline that populates it never
actually uses them: `FakeMarketDataProvider.instrument_master()`
(`backend/src/data/providers.py`) hardcodes `instrument_type="equity"` for
every symbol it's given, with a literal `_ = rng  # reserved for future
F&O instrument generation` comment marking the gap. **Precise finding:**
the instrument master's *schema* supports options; its only real *sync
provider* has never generated a single options row for any underlying.
This is a provider-implementation gap, not a schema gap, and not the kind
of "needs a vendor we don't have" gap Follow-up D's original (already
corrected) finding turned out to be — this one is exactly what it looks
like.

That gap turned out not to block anything, because Follow-up D's fix
never needed the instrument master in the first place: Upstox's real
`/option/chain` endpoint (`UpstoxAdapter.get_option_chain`, wired in
Follow-up D) already returns strikes with real CE/PE data live from the
broker, which is a better source than a nightly-synced local table would
be regardless of whether that table carried options. Steps (a)/(b)/(c) of
this pass's plan (resolve strikes, batch-fetch quotes, assemble a
response with the ATM strike marked) were already almost entirely done by
Follow-up D — only ATM marking was genuinely missing.

**Fix:** `GET /api/v1/market-data/option-chain/{underlying}`
(`backend/src/api/routes/market_data.py`) now does one extra, best-effort
step after fetching the chain: `adapter.get_quote(underlying)` for the
underlying's own spot price, then marks the strike closest to that spot
as ATM. `LiveOptionChainResponse` gained `underlying_ltp`/`atm_strike`
(both `float | None`) — a failed spot lookup (bad instrument key, broker
error) leaves both honestly `None` rather than failing the whole
already-real per-strike response. `app/analysis/page.tsx`'s Live Option
Chain panel now shows the spot price and highlights the ATM row (reusing
`.atm-row`/`.atm-tag` CSS classes the original v0 build had already
prepared for this and left unused, same discovery Follow-up C made for
`.technical-layout`).

**Step 3 — the precise, named remaining gap**, per broker:

- **Upstox: no gap.** OI and IV are both real and already wired
  (`market_data.oi` / `option_greeks.iv` from Upstox's v2 API), confirmed
  live in Follow-up D's own pass and unchanged here.
- **Zerodha: RESOLVED (follow-up pass immediately after this one), OI real,
  IV permanently unavailable.** The original gap was more precise than
  the adapter's previous error message claimed: Kite Connect has no bulk
  option-chain or options-greeks endpoint at all — confirmed by Zerodha's
  own public API documentation as read from training knowledge, and by
  this adapter's own (then-`NotImplementedError`) message. That message
  used to blame "the NFO instrument master... not available yet",
  implying the blocker was Phase 10 infrastructure this codebase lacks —
  misleading in exactly the way this pass's own instructions warned
  against. Fixed in the immediate follow-up: `ZerodhaKiteAdapter.
  get_option_chain`/`get_expiries` now download and cache the real NFO
  instrument dump (`GET /instruments/NFO`, a CSV of every tradingsymbol/
  strike/expiry, cached at module level for 15 minutes since it doesn't
  change intraday and a fresh adapter is constructed per call), resolve
  per-strike tradingsymbols for the requested underlying/expiry, and
  batch-quote them via `GET /quote` (multiple `i=` params in one call,
  well within Kite Connect's documented 500-instrument cap for a single
  expiry's CE+PE legs) to get real LTP and OI. **What remains permanently
  missing**: implied volatility. Kite Connect's quote response has no IV
  or options-greeks field of any kind — `call_iv`/`put_iv` stay `None`
  for this broker forever, never Upstox-parity, and this adapter itself
  never invents one. A separate, later pass (below) fills that specific
  gap at the API layer with a clearly-labeled *computed* estimate, never
  by changing what this adapter itself honestly reports. 10 new/updated
  backend tests (`backend/tests/test_brokers_zerodha.py`,
  `backend/tests/test_market_data_api.py`) cover: real LTP/OI extraction,
  IV always null, an unmatched underlying/expiry returning an honestly
  empty list, a strike with only one leg listed, sorted/deduplicated
  expiries excluding futures and other underlyings, the module-level
  cache genuinely being reused across calls (one CSV download for three
  calls in the same process), and a real `BrokerServerError` surfacing
  correctly from the instrument-dump fetch. **Live-verified** the same
  way as Upstox above: saved real-shaped (fake) Zerodha credentials via
  the real `POST /api/v1/broker-credentials/zerodha` endpoint and hit the
  option-chain endpoint — a genuine outbound HTTPS request to
  `api.kite.trade/instruments/NFO`, failing only with
  `httpx.ProxyError: 403 Forbidden` from this sandbox's own egress
  policy, the identical evidence pattern already established for Upstox.

6 new/updated backend tests (`backend/tests/test_market_data_api.py`)
cover the ATM-marking work itself: real Upstox OI/IV/LTP across three
strikes with a real spot lookup correctly picking the middle strike as
ATM, and a spot-lookup failure leaving `underlying_ltp`/`atm_strike` null
without failing the chain itself.

**Live-verified** against a real `uvicorn` + local Postgres/Redis (this
sandbox's only viable verification path): hit the endpoint with no broker
configured over real HTTP and got the honest `503`; saved real-shaped
(fake) Upstox credentials via the actual `POST
/api/v1/broker-credentials/upstox` endpoint and hit the option-chain
endpoint again — it made a genuine outbound HTTPS request to
`api.upstox.com` and failed only with `httpx.ProxyError: 403 Forbidden`
from this sandbox's own egress policy, the exact traceback captured in
the live `uvicorn` log, the same "real attempt, sandbox-blocked" evidence
Phases 8/9/16 already established. Demo user and credentials deleted
afterward.

### Part 2's "Step 1" finding — resolved in a later pass

The literal `_ = rng  # reserved for future F&O instrument generation`
gap named above (`FakeMarketDataProvider.instrument_master()` never
generating an options row for any underlying) was left unfixed at the
time since Follow-up D's live option chain never needed it. Fixed in a
later pass, once picked up as a standalone item: each equity symbol now
also gets a monthly futures contract plus a 5-strike option chain
(CE/PE) per upcoming monthly expiry — the last Thursday of the month,
rolled back to a real NSE trading day via `previous_nse_trading_day`
when that Thursday is a holiday, never an invented date — with strikes
centered on the symbol's own latest synthetic close (the same real
anchor `intraday_minute_bars` already uses) rather than an arbitrary
fixed price. `isin` stays `None` for every F&O row, since NSE genuinely
never assigns one to a derivative contract. 11 new tests
(`backend/tests/test_data_providers.py`) cover the expiry-rollback rule
against the real calendar, the strike-step/ATM-centering logic, one
shared lot size across a symbol's whole F&O chain (matching real NSE
convention), symbol uniqueness within the 32-char column width, and
determinism.

This was previously a genuinely dead frontend feature, not a
speculative one: `app/analysis/page.tsx`'s `OptionChainTab` ("Option
instrument master" panel) already filtered
`instrument_type === 'option'` and derived its underlying-symbol
dropdown from the instrument master — built correctly, but permanently
rendering "No option instruments found for this underlying" since the
backend had never produced a single row for it to show. No frontend
change was needed; it was only ever waiting on real backend data.

**Live-verified** against a real `uvicorn` + local Postgres/Redis:
`POST /api/v1/market-data/ingest/instrument-master` for `RELIANCE`
genuinely ingested 23 rows (1 equity + 2 monthly futures + 20 options,
10 CE/10 PE across 2 expiries) over real HTTP, and `GET
/api/v1/market-data/instruments` returned them with the exact shape
`OptionChainTab` consumes (`symbol`, `strike_price`, `option_type`,
`expiry_date`, `underlying_symbol`, `lot_size`, `tick_size`) — confirming
that panel now genuinely renders real rows instead of the empty-state
message it always showed before. Full backend suite: 849 passed;
`ruff format --check`/`ruff check` clean. Rows, provenance record, and
demo user deleted afterward.

### Zerodha's permanently-missing IV — closed with a labeled, computed estimate

The one gap named as permanent above (Kite Connect has no options-
greeks field at all, so `call_iv`/`put_iv` stay `None` for Zerodha
forever) is now filled, without touching that honesty at all: a wholly
new `src.engine.options_pricing` module (no Black-Scholes code, risk-
free-rate setting, or time-to-expiry helper existed anywhere in this
codebase before this) solves for implied volatility by bisection over
the real Black-Scholes-Merton price formula — dependency-free (a hand-
rolled normal CDF via `math.erf`, no new `scipy` dependency), since
Black-Scholes price is strictly increasing in volatility so bisection
needs no vega/derivative computation and is always well-behaved.
`GET /api/v1/market-data/option-chain/{underlying}`
(`backend/src/api/routes/market_data.py`) computes it only when the
broker itself reported no IV and a real spot price + forward-looking
expiry are both available, filling two **new, separate** response
fields — `call_iv_computed`/`put_iv_computed` — never overwriting or
blending with `call_iv`/`put_iv` themselves, so a client can never
mistake a modeled number for Upstox's real broker-reported one. A new
`risk_free_rate` setting (default 7%, `backend/src/core/config.py`) is
the one necessary modeling assumption every IV solver needs — documented
as exactly that, a configurable input, never presented as a measured
figure. Every genuinely un-computable case (non-positive price/spot/
strike, an already-elapsed or same-day expiry, a market price outside
what any volatility in a sane 0.1%–500% range can produce, or a solve
that doesn't converge) returns `None`, never a guessed number.
`app/analysis/page.tsx`'s Live Option Chain table shows a computed value
with a small "CALC" tag and a tooltip explaining it's Black-Scholes-
derived, not broker-reported (reusing the existing `.atm-tag` CSS class)
— real IV cells render exactly as before, unchanged.

15 new/updated backend tests
(`backend/tests/test_engine_options_pricing.py`,
`backend/tests/test_market_data_api.py`) cover: a full round-trip
(pick a known volatility, compute its real Black-Scholes price, solve
IV back from that price, confirm it recovers the original volatility)
across a wide range of moneyness and volatility levels, a real put-call
parity identity check independent of the round-trip, every un-computable
case honestly returning `None`, the live HTTP route computing a real
Zerodha IV that round-trips through the exact same math end to end, the
existing already-elapsed-expiry Zerodha test now also asserting the
computed fields stay `None`, and Upstox's real-IV test now asserting the
computed fields stay `None` too (the solver must never run, let alone
override, when a real value already exists). Full backend suite: 874
passed; `ruff format --check`/`ruff check` clean; `tsc --noEmit` clean.

**Live-verified** against a real `uvicorn` + local Postgres/Redis: the
app started and routed requests correctly with the new module/route
wired in (no import or startup errors), and a real-shaped (fake)
Zerodha credential's option-chain request reached exactly as far as the
NFO instrument-dump fetch before failing with the same
`httpx.ProxyError: 403 Forbidden` this sandbox's own egress policy has
produced for every other Zerodha network attempt in this project
(Phases 8/9/16 and this same Phase 17 pass) — a pre-existing sandbox
limitation this pass didn't introduce or need to work around, confirming
the new code executes correctly right up to the same real network
boundary. The actual IV computation itself is verified by the mocked-
transport tests above, the same "real math against a mocked broker
response, real network attempt to prove the wiring" split this project
uses throughout. Demo user and saved credentials deleted afterward.

## Part 3 — System Vitals

The Phase 16 Follow-up E design deliberately stopped short: it read
`/metrics`'s own real Prometheus `Counter`/`Histogram` objects via
`.collect()` and reported them honestly labeled "since process start" —
correct, but not what an Overview panel titled "System Vitals" should
show for LLM token usage ("today") or order-dispatch latency (a live
health signal, not a lifetime average). This pass replaces that design
per the resolution the real-world testing plan specified, rather than
layering a second endpoint alongside it.

- **Host CPU/memory**: `backend/src/observability/vitals.py::get_host_vitals()`
  reads `psutil.cpu_percent()`/`psutil.virtual_memory()` directly against
  the API process's own OS view — a genuine current-value gauge, added as
  a new runtime dependency (`psutil>=6.0,<7.0`, `backend/pyproject.toml`).
  This is the first time this codebase has ever reported host metrics
  anywhere; Follow-up E's own pass had explicitly left this out because
  nothing in the codebase tracked it, which was true until now.
- **LLM token usage "today"**: deliberately *not* derived from the
  existing `tradingos_llm_token_usage_total` Prometheus counter (that
  counter's own semantics — cumulative since process start — are exactly
  why Follow-up E never wrapped it in a "today" label). Instead,
  `src.agents.llm_router.LlmRouter._record_usage` (the same single choke
  point that already feeds the Prometheus counter, both `complete()` and
  `stream_complete()`) now also calls
  `vitals.record_token_usage_today(get_redis(), provider, tokens)` on
  every real completion — an `INCRBY` against
  `tokens:{provider}:{YYYY-MM-DD}` (Asia/Kolkata calendar day, matching
  Follow-up F's own "today" convention for an Indian-markets operator),
  with a 3-day `EXPIRE` so stale per-day keys don't accumulate forever.
  The Prometheus counter is untouched — this is a second, small write
  path alongside it, exactly as specified, not a replacement.
- **Order-dispatch latency**: `vitals.get_order_dispatch_latency_percentiles()`
  queries Prometheus's own HTTP API (`GET {prometheus_url}/api/v1/query`)
  with `histogram_quantile(0.50, rate(tradingos_order_dispatch_latency_seconds_bucket[5m]))`
  and the 0.95 equivalent — the same query Grafana itself would run —
  reusing the existing histogram
  (`src.brokers.resilient.ResilientBrokerAdapter` already feeds it on
  every real dispatch) rather than building a second latency-tracking
  path. A new `prometheus_url` setting (`backend/src/core/config.py`,
  default `http://localhost:9090`, overridden to `http://prometheus:9090`
  in `docker-compose.yml`'s `backend` service) points at it. Prometheus
  unreachable — which is every case in this sandbox, since (per
  `docker-compose.yml`'s own long-standing comment) Docker Hub pulls have
  never worked here — reports both percentiles as `None`, never a
  fabricated number.
- **Market**: `is_open` reuses the existing `is_market_open_ist`; a new
  `next_market_event()` (`backend/src/engine/paper_trading/market_hours.py`)
  returns the next real IST market-hours boundary (an open or a close),
  walking forward through `is_nse_trading_day` the same way
  `previous_nse_trading_day` already walks backward.

`GET /api/v1/system/vitals` (`backend/src/api/routes/system.py`) replaces
`GET /api/v1/observability/vitals` entirely (`backend/src/api/routes/
observability.py` and its old `SystemVitalsResponse`/`build_vitals_summary`
deleted, not left running alongside the new one) and returns exactly the
shape this pass specified: `{host, llm, latency, market, as_of}`, with
`latency.budget_ms` read from the real `settings.order_dispatch_latency_budget_ms`
(confirmed `500.0`, not assumed) rather than hardcoded. Same authenticated
read-only RBAC posture (`register_policy(..., roles=list(Role))`) as every
other read surface in this codebase — never the unauthenticated `/metrics`
scrape convention. `app/page.tsx`'s `Vitals()` component is rewired to the
new shape with honestly distinct copy per figure: host numbers as
"current", token usage explicitly "today", latency explicitly labeled
with its real trailing window (`p95 {value}ms` / `trailing 5m · budget
{value}ms`), replacing the old "not exposed yet" tiles the design never
actually needed once real data existed for all four groups.

21 new/updated backend tests (`backend/tests/test_observability_vitals.py`,
`backend/tests/test_api_system.py`) cover: real host-gauge plausibility
bounds, a real Redis round-trip for token usage (including the
zero/negative-token no-op case and a provider honestly absent rather than
a fabricated `0`), Prometheus response parsing against a mocked transport
(a real value, an empty window, and an unreachable server — all three
paths), every `next_market_event` boundary (before open, during session,
after close, a weekend), the full assembled shape, and RBAC across all 4
roles over real HTTP.

**Live-verified** against a real `uvicorn` + local Postgres/Redis:
registered a demo `SystemAdministrator`, hit `GET /api/v1/system/vitals`
over real HTTP, and got real, sensible numbers —
`{"cpu_percent": 9.5, "memory_percent": 6.6, "memory_used_mb": 655.2,
"uptime_seconds": 15.9}` for host, `{"anthropic": 180, "openai": 300,
"gemini": 60}` for `token_usage_today` (genuinely written by this same
pytest run's own LLM-router tests hitting the same local Redis, not
seeded — direct proof the Redis write path this pass added is real, not
just unit-tested in isolation), `order_dispatch_p50_ms`/`p95_ms` both
honestly `null` (Prometheus unreachable in this sandbox, as expected),
and `market.next_event_at` correctly resolving to the next day's `09:15
+05:30` open given the request landed after market close. Demo user and
polluted Redis keys deleted afterward. Full backend suite: 818 passed;
`ruff format --check`/`ruff check` clean on `backend/`; `tsc --noEmit`
clean on the frontend.

## Parts 1 and 4 — blocked, then fixed, on the first real local attempt

The user did attempt Parts 1/4 at `D:\TradingOS-2.0\TradingOS-2.0` via
`docker compose up`, and hit a real, blocking bug before any of it could
start: every Settings panel (Broker Config, LLM Providers, Notification
Channels) showed "Not configured" with every Save disabled, and Broker
Config's error banner showed the literal text `SECRETS_ENCRYPTION_KEY is
not configured...` verbatim. This section documents the root-cause
diagnosis and fix, not a guess — each root cause below was confirmed by
reading the actual code before being called the cause.

### Root cause 1 — not docker-compose.yml, the root `.env.example`

`docker-compose.yml`'s `backend` service already passed
`SECRETS_ENCRYPTION_KEY` through correctly
(`SECRETS_ENCRYPTION_KEY: ${SECRETS_ENCRYPTION_KEY:-}`, the same pattern
as `JWT_SECRET_KEY`) — that was never the bug. The real gap: the repo-root
`.env.example` (the one a Docker Compose user actually copies to `.env`)
had **no `SECRETS_ENCRYPTION_KEY` entry at all**, while `backend/.env.example`
(for a bare, non-Docker `uvicorn` run) documented it fully with the exact
generation command. A user following the documented Docker path had no
way to discover the variable existed. All three Settings stores
(`secrets_store.py`, `llm_provider_store.py`, `channel_store.py`) share
this one Fernet key and fail with the identical message text, which is
why all three panels broke identically.

**Fixed:** `.env.example` now documents `SECRETS_ENCRYPTION_KEY` with the
generation command, matching `backend/.env.example`'s existing model.
`backend/scripts/api-entrypoint.sh` (shared by both `docker-compose.yml`'s
`backend` service and `Dockerfile.allinone`'s `[program:api]`) now prints
a loud, non-fatal startup warning naming the exact panels affected and the
exact fix if the key is empty — deliberately **not** a hard `exit 1`: the
codebase's existing design already treats this key as optional (paper
trading and most other features work fine without it), and the user,
asked directly, chose the non-fatal-warning design over changing that
posture. `README.md` and `docs/CLAUDE.md`'s Development & Testing Workflow
section both now call this out as an explicit first-time-setup step.

### Root cause 2 — a real unhandled-exception bug in local/custom LLM providers

Separately, real: `OllamaClient.complete`/`CustomProviderClient.complete`
(`backend/src/agents/llm_router.py`) and every branch of `_discover_models`
(`backend/src/api/routes/llm_providers.py`) made raw `httpx` calls with no
`except httpx.HTTPError` guard — only an HTTP-status failure was ever
converted to a friendly `LlmProviderError`; a connection failure (exactly
what `http://localhost:11434` produces from inside the backend's own
Docker container, since `localhost` there means the container itself, not
the host machine running Ollama) was an **unhandled exception**, surfacing
as a raw 500 rather than a graceful "Test Connection failed" message.
Confirmed live in this pass: saving `http://127.0.0.1:1` (a real,
guaranteed-unreachable loopback port, standing in for the exact Docker
`localhost` trap) as a Custom provider's base URL and hitting `/test`
returned `{"ok": false, "detail": "custom: connection failed: All
connection attempts failed"}` over real HTTP once fixed — a genuine
network failure, handled gracefully, not mocked.

**Fixed:** `OllamaClient.complete`/`CustomProviderClient.complete` now
catch `httpx.HTTPError` and convert it to a real `LlmProviderError`,
matching the exact pattern already used everywhere else in this codebase
for outbound calls (`src.notifications.senders`,
`src.api.routes.broker_oauth`). `test_llm_provider_endpoint` and
`list_llm_provider_models_endpoint` (`llm_providers.py`) both also now
catch `httpx.HTTPError` as a safety net covering every other provider
client's `complete()`, not just the two named above (the same gap exists,
e.g., in `AnthropicClient.complete`, confirmed by reading it — left
uncaught at the client level since that's a pre-existing pattern this
pass didn't otherwise touch, but now covered by the route-level net
either way). `docker-compose.yml`'s `backend` service gained
`extra_hosts: ["host.docker.internal:host-gateway"]`, required for
`host.docker.internal` to resolve at all under Linux Docker Compose
(Docker Desktop on Windows/Mac resolves it without this). The Settings
UI's base-URL field placeholder, which previously read the literal
(broken) `http://localhost:11434`, now reads the correct
`http://host.docker.internal:11434`, with explicit help text underneath
explaining why — the explicit-help-text approach, not a silent
auto-rewrite of what the user typed, matching this codebase's existing
idiom (no URL-rewrite utility exists anywhere else in it either).

11 new/updated backend tests
(`backend/tests/test_agents_llm_router.py`,
`backend/tests/test_llm_providers_api.py`) cover: a genuine connection
attempt against an unreachable loopback port for both `OllamaClient` and
`CustomProviderClient` (no mock transport needed — loopback traffic never
touches this sandbox's egress proxy, so this is a real network failure,
not a simulated one), and the `/models` route converting a mocked
`httpx.ConnectError` into a real 502 rather than an unhandled 500.

### Part 3 (Zerodha vs Upstox) — confirmed live, no code gap

Investigated before assuming a gap: `backend/src/api/routes/broker_oauth.py`
already implements both brokers fully and symmetrically (Zerodha's real
Kite Connect login URL and the real SHA-256 checksum'd token exchange;
Upstox's real authorization-code exchange), and the frontend
`BrokerConfigCard` (`app/settings/page.tsx`) already renders the same
redirect-URL box and the same working "Connect" button for both brokers.
**Live-confirmed in this pass** (not just read): `GET
/api/v1/broker-credentials` initially showed `redirect_uri: null` for a
freshly-saved Zerodha credential; calling `GET
.../zerodha/login-url` once (exactly what clicking "Connect" does)
returned the real `https://kite.zerodha.com/connect/login?v=3&api_key=...`
URL and persisted `redirect_uri`, after which the list endpoint showed it
populated — precisely the behavior the earlier static-code investigation
predicted. The screenshot's original asymmetry (Upstox showing guidance
text, Zerodha showing the encryption-key error instead) is fully
explained by Root Cause 1 above plus Zerodha's Connect never having been
clicked yet; **no Zerodha-specific code change was needed or made.**

### Part 4 (notifications) — same root cause as Part 1

`backend/src/notifications/channel_store.py`'s `get_notification_channel_store()`
has the identical `SECRETS_ENCRYPTION_KEY` guard as the broker/LLM stores
— expected to resolve once the user sets a real key locally, per Root
Cause 1's fix. Not independently re-broken by anything in this pass; live
confirmation (saving a real channel, sending a real test message) is the
user's own next step per Part 4 of their original request.

**Live-verified in this sandbox** (its own Postgres/Redis, the same path
used throughout this project — Docker itself still cannot run here):
registered a demo `SystemAdministrator`, saved a Custom LLM provider
pointed at a real unreachable loopback port, and confirmed both `/test`
(graceful `ok: false`) and `/models` (real `502`) over genuine HTTP with
a genuine network failure underneath, not a mock. Saved real-shaped
Zerodha credentials and confirmed the redirect-URL generate-on-first-call
behavior end to end. Full backend suite: 827 passed; `ruff format
--check`/`ruff check` clean; `tsc --noEmit` clean. Demo user, saved
credentials, and secrets-store files deleted afterward. **Not verified
here, and still the user's own next step on `D:\TradingOS-2.0\TradingOS-2.0`**:
a real Docker rebuild picking up these fixes, a real local Ollama
instance passing Test Connection through the actual container, a real
completed Zerodha/Upstox OAuth login, and a real notification test
message arriving — all now unblocked, none of them previously reachable
before this pass.

## Overview "System Vitals" — LLM provider health, the last genuinely stale gap

Phase 16 Follow-up E explicitly left "per-provider LLM health" unresolved
("likewise still genuinely not exposed and stays labeled as such"), and
this pass's own `GET /api/v1/system/vitals` rebuild (above) didn't touch
it either — the Overview page's LLM tile kept a literal "Provider health
is not exposed yet — see docs/CLAUDE.md gaps." caption even after every
other vitals figure went real. This was verified to still be a real gap,
not stale copy describing an already-fixed problem: `grep` across every
API route confirmed nothing ever read `LlmRouter.health_for()`, even
though the router has tracked real per-provider `ProviderHealth`
(`last_failure_at`, `last_success_at`, `served_as_fallback`) since Phase
3, updated on every real completion call by `orchestration/strategies.py`,
`orchestration/chat.py`, `orchestration/strategy_suggestions.py`, and
`notifications/inbound_router.py`.

**Fixed:** `LlmRouter.fallback_order()` (`backend/src/agents/llm_router.py`)
is a new public read-only wrapper around the existing private
`_fallback_order()` (no preferred-provider override), and
`llm_provider_health_vitals()` reads the real, process-wide
`get_llm_router()` singleton — the same instance every real caller above
already mutates, never a fresh, always-empty router — and returns each
configured provider's health, in the router's live fallback order,
converting the internal `time.time()` epoch floats to ISO-8601 timestamps
(`None` stays `None`, never a fabricated timestamp for a provider that
hasn't been called yet). `GET /api/v1/system/vitals`
(`backend/src/api/routes/system.py`) now passes this into
`build_system_vitals()` as a new `llm.provider_health` field
(`LlmProviderHealthVitals` in `backend/src/api/schemas.py`). The Overview
page's LLM tile (`app/page.tsx`) replaces the stale caption with a real
per-provider status row (a colored dot — green for a real success, red
for a more-recent real failure, dim for genuinely never called — plus a
"(fallback)" tag when the provider's last success was served as a
fallback), with the real timestamp as the row's tooltip.

4 new/updated backend tests
(`backend/tests/test_agents_llm_router.py`,
`backend/tests/test_observability_vitals.py`,
`backend/tests/test_api_system.py`) cover: the real singleton router's
health surfacing correctly after a real success and a real failure (via
injected fake clients, same pattern as this module's other router tests),
a provider that's never been called reporting all-`None`/not-fallback
rather than a fabricated default, the field's shape flowing correctly
through `build_system_vitals()`, and the live HTTP route returning a
non-empty `provider_health` list with the exact documented shape for
every entry. Full backend suite: 829 passed; `ruff format --check`/`ruff
check` clean; `tsc --noEmit` clean on the frontend change.

Phase 16 Follow-up E (`docs/phase16-wiring-audit.md`) is updated to mark
this specific sub-gap resolved rather than leaving stale text claiming it
is still open.

## Settings still failing after the first fix — second diagnosis (2026-09-23, local Docker)

The first fix (above) did not fix Settings on `D:\TradingOS-2.0\TradingOS-2.0`.
This pass diagnosed it from evidence gathered on the running stack before
changing any code.

### Evidence

- Checkout: `main` == `origin/main` (`efb43e2`). The one local change was
  `config/tradingos.config.json`, rewritten at runtime by the app itself.
- **No root `.env` existed.** Only `.env.example` and `backend/.env.example`
  (where the key is commented out). The key was also absent from the shell
  and from the Windows User/Machine environment.
- Inside the running backend: `SECRETS_ENCRYPTION_KEY` length **0**.
  `CORS_ORIGINS=http://localhost:3003`, which matched the frontend's published
  port. The frontend bundle had `http://localhost:8000` baked in, which is
  correct. So this was not CORS and not a stale `NEXT_PUBLIC_API_URL`.
- Real JWT (SystemAdministrator), curl, Origin `http://localhost:3003`:
  - `OPTIONS /api/v1/settings/llm-providers/ollama` → 200, allow-origin
    matches.
  - `POST /api/v1/settings/llm-providers/ollama` → **503**
    `{"detail":"SECRETS_ENCRYPTION_KEY is not configured -- ..."}`.
  - `POST /api/v1/broker-credentials/zerodha` → the same 503.
  - `GET /api/v1/settings/llm-providers` → the same 503.
- Backend log for that request (`POST ... 503 Service Unavailable`) was
  only in `/var/log/supervisor/api.log` **inside the container**.
  `docker compose logs backend` showed nothing but supervisord's own
  `spawned`/`RUNNING` lines. The startup warning added by the first fix
  *had* fired, in that same in-container file, where no one would see it.
- The only real user, the operator's own account, is SystemAdministrator.
  This was not RBAC.

### Actual root cause

Every Settings read/write returned 503 because `SECRETS_ENCRYPTION_KEY` was
empty in the container: no `.env` existed at the repo root for compose to
read. The first fix documented the variable and added a warning, but the
warning, like every API log line, was invisible: supervisord sent the
`api` program's stdout to a file inside the container, not to
`docker compose logs`.

A second, latent bug would have hit right after the key was set. All three
encrypted stores (`broker_credentials.enc`,
`llm_provider_credentials.enc`, `notification_channels.enc`) live in
`/app/secrets` in the container's writable layer, with **no volume**. The
documented `docker compose down && up --build` cycle would have silently
wiped every saved broker/LLM/notification credential on each recreate.

### Fix

- Local only (gitignored, never committed): created `.env` from
  `.env.example` with a freshly generated Fernet key. **Back up this key
  together with the `tradingos-2.0_secrets` volume.** The stores cannot
  be decrypted without it.
- `backend/supervisord.conf` and `deploy/allinone-supervisord.conf`:
  `[program:api]` now logs to `/dev/stdout` / `/dev/stderr` (with
  `*_logfile_maxbytes=0`). The key warning, request logs and tracebacks now
  appear in `docker compose logs backend` and in `pnpm docker:up`'s
  attached output.
- `docker-compose.yml`: named volume `tradingos-2.0_secrets` mounted at
  `/app/secrets`.
- `backend/src/brokers/tick_source.py`: logs `tick_source.selected` with
  `source=mock|broker_quotes` at startup. Previously nothing in the logs
  said which feed was active.
- Regression tests: `backend/tests/test_container_settings_persistence.py`
  checks api→stdout in both supervisord configs, the secrets volume mount
  and declaration, and that all three store paths resolve under
  `/app/secrets`. Verified to **fail on the pre-fix files** (2 failures)
  and pass after.

### Verified after the fix (full `docker compose down` + `pnpm docker:up -d`)

- All six services up: backend/prometheus/redis/temporal healthy, frontend
  and grafana started. No errors in any service's logs. Key length 44
  inside the backend.
- `POST /api/v1/settings/llm-providers/ollama`
  (`http://host.docker.internal:11434`) → **204**. `GET` reads it back as
  `configured: true`. `/app/secrets/llm_provider_credentials.enc` written
  with mode 0600. Request log visible in `docker compose logs backend`.
- **Survives a full `docker compose down` + recreate** (new container,
  value still present).

### Parts 3–4 results against the real stack

| Item | Result |
|---|---|
| 3.1 LLM providers | **Ollama: passes.** `qwen2.5:0.5b` pulled. `GET .../ollama/models` lists it via `http://host.docker.internal:11434` from inside the backend container. `POST .../ollama/test?model=qwen2.5:0.5b` → `ok: true` (10.9 s, cold model load). Hosted provider: **not yet done** (needs the operator's real API key). |
| 3.2 Broker OAuth (Zerodha, Upstox) | **Not yet done**: needs the operator's developer-console redirect registration and real logins. Both brokers show `never-connected`. |
| 3.3 Notification channel | **Not yet done**: needs a real bot token. |
| 4.1 Live ticks | **Not verifiable yet.** No broker connected, and NSE was closed at test time (next open 09:15 IST). Also a **real gap**: `build_tick_source()` is resolved once in the lifespan, so a broker connected via Settings only drives paper trading after a **backend restart**. The new `tick_source.selected` log line shows which feed is active. |
| 4.2 Live option chain | **Not verifiable yet.** Same broker/market-hours dependency. |
| 4.3 System Vitals | `GET /api/v1/system/vitals` returns real host CPU/memory/uptime and market state. `token_usage_today` is empty and p50/p95 are `null`: no LLM call or order dispatch has happened on this fresh stack, not fabricated zeros. Changes over time and UI labelling not yet eyeballed. |
| 4.4 Paper trade end-to-end | **Passes after the sandbox fix below.** As PortfolioManager: `POST /strategies` 201 → enroll `DEMOSTOCK` 201 → daily-signal-run BUY @ 106.12 → tick → **filled 47 @ 106.17**. It appears in `GET /orders` (`mode: paper, status: filled`) and in the audit log (create, enroll, signal runs, tick). The manual endpoints use the fake daily-price provider (documented honest stub), so this exercises the real engine but not real prices. |
| 4.5 Kill Switch | Tripped via a real drawdown observation (`POST /kill-switch/paper/check`, 20% ≥ 15%), then reset by RiskManager. PortfolioManager reset correctly refused (403). **Trip and reset both arrived live on `/api/v1/ws/activity-feed`** while subscribed. Visual check of the Organization Pulse orb and System Vitals panel is **not yet done** (needs a logged-in browser session). Left in the reset state. |
| 4.6 Four roles | Four fixture accounts (`e2e.admin/pm/risk@example.com` via `scripts/seed_e2e_users.py`, plus `e2e.auditor@example.com` via `/auth/register`). Settings reads are allowed for all four roles. Broker/LLM/notification writes are SystemAdministrator-only (others 403). Audit entries are admin + auditor only. Consistent with every `register_policy`. Audit chain verifies (`db_chain_valid: true`). |

Two more issues surfaced in the now-visible logs:

- A browser tab still open on the frontend's *previous* port (3003) got
  `OPTIONS ... 400` CORS preflight rejections after `docker-up.js` moved
  the frontend to 3002. After every `pnpm docker:up`, use the frontend URL
  it prints, not an old tab.
- Postgres is published as `0.0.0.0:<random>->5432` when
  `POSTGRES_HOST_PORT` is unset, despite the compose comment saying
  nothing is published. **Fixed** in this pass: removed from
  `docker-compose.yml` and moved to the opt-in
  `docker-compose.postgres-port.yml` (loopback via `BIND_HOST`). Verified
  live: after `docker compose down` + `docker-up.js -d`, `docker compose ps`
  shows no 5432 mapping and every published port is on `127.0.0.1`.

During the RBAC sweep, an admin write probe briefly stored placeholder
values for `deepseek` (LLM) and `upstox` (broker). Both were deleted within
a minute and both actions are in the audit log. The only credential left
configured is the Ollama base URL.

### Strategy sandbox fix (operator chose: custom seccomp profile)

Root cause, verified live: creating a network namespace needs
`CAP_SYS_ADMIN` **unless** it is created inside a new user namespace.
Docker withholds that capability, and its default seccomp profile also
gates `unshare()` behind it. Even with seccomp fully unconfined, a bare
`unshare --net` still fails. So a seccomp change alone could never have
fixed it.

- `src/engine/sandbox/process_runtime.py`: `UNSHARE_NET_ARGS` is now
  `unshare --user --map-root-user --net --`. No added capability.
- `deploy/seccomp/backend.json`: Docker's own default profile
  (`moby/profiles` `seccomp/default.json`, unmodified) plus **one** rule:
  `unshare` allowed only when `(flags & 0x2e020000) == 0`, i.e. user and
  network namespaces only (the same masked-flags style moby uses for
  `clone`). Applied via `security_opt` on the backend service only.
  `cap_add: SYS_ADMIN` was considered and rejected as far broader.
- Verified in a throwaway container under the profile: network
  unreachable inside the worker (`[Errno 101]`, the same proof the sandbox
  was originally verified with). `--mount` and `--pid` namespaces still
  denied. Bare `--net` still denied. Normal networking outside the sandbox
  unaffected.
- `Dockerfile.allinone` header documents that a bare `docker run` needs
  the same `--security-opt` and a `/app/secrets` volume.
- Regression tests: `backend/tests/test_docker_sandbox_seccomp.py`.

### Backend test suite on this machine

Ran in a throwaway container from the backend image, on a copy of the repo
(not the live `config/`), against the stack's Postgres (`tradingos_test`)
and Redis (DB 15), under the new seccomp profile: **894 passed, 3 failed**.
`ruff check` and `ruff format --check` are clean. The 3 failures are
environmental:

- 2 × `test_audit_archive.py`: the test's own cleanup calls `chattr`,
  which the runtime image lacks. Production code
  (`src/audit/archive.py::_try_make_append_only`) already handles this
  (catches `OSError`, logs `audit.archive_chattr_unavailable`).
- 1 × `test_market_data_api.py::test_bhavcopy_fallback_...`: this machine
  has real internet and fetched the real NSE bhavcopy
  (`nse_bhavcopy_real`); the test assumes no egress.

The first run of the suite, without the profile and with Redis on
`localhost`, showed 89 failed / 15 errors: 66 were this sandbox bug and 35
were Redis being unreachable from the harness. The numbers above are the
corrected run.

## Broker Config: redirect URL chicken-and-egg, post-login 404, top-bar overlap

Reported from the live console after the Settings fix:

- **Redirect URL only appeared after "Connect", and Connect needs a saved
  API key.** Zerodha and Upstox only issue that key *after* a redirect URL
  is registered in their developer console. `GET /api/v1/broker-credentials`
  now always returns the effective `redirect_uri` (plus
  `default_redirect_uri` and `redirect_uri_is_custom`), even with nothing
  saved. The URL is editable: it can be sent with the first key save (the
  `redirect_uri` field on `POST .../{broker}`), or changed/reset later via
  the new `PUT .../{broker}/redirect-uri` (SystemAdministrator only)
  without re-entering the write-only secrets. Only the origin (plus any
  reverse-proxy path prefix) can change: the path must still end at
  `/api/v1/broker-credentials/{broker}/callback`, or the login redirect
  would never reach the token exchange. Connect (`login-url`) uses the
  custom URL, and Upstox's token exchange sends the same one. Re-saving
  keys keeps a custom URL. The OAuth routes now rebuild stored credentials
  with `dataclasses.replace`, so no field gets silently dropped.
- **After a successful broker login the browser landed on a 404.**
  `_settings_redirect` used the backend's own origin
  (`http://localhost:8000/settings`, a 404 in the compose stack). New
  `FRONTEND_BASE_URL` setting, set by `docker-compose.yml` from
  `FRONTEND_HOST_PORT` so it follows `docker-up.js`'s port bumps. Unset
  (the single-origin all-in-one image) keeps the old same-origin behavior.
  Verified live: it lands on `http://localhost:3002/settings?...`.
- **Top bar overlapped scrolled content.** The fixed top bar used
  `--glass-bg` (`rgba(15,20,25,.4)`) with no backdrop blur, so the Settings
  status strip showed straight through it. It is now `.shell-topbar` (90%
  `--background` + 18px blur, same recipe as the mobile tab bar).
  `.shell-main` also used `padding-top: 60px` under a 64px (`h-16`) bar;
  that is now `4rem`.

Tests: `backend/tests/test_broker_redirect_uri.py` (10). Broker/OAuth/RBAC
suites: 104 passed. `ruff` clean. `tsc --noEmit` clean (run in the
frontend's deps image; `next build` ignores type errors). The broker card's
look is not yet verified in a logged-in browser.

## LLM Providers: Ollama missing from the chain, "auto" never resolved, Hugging Face

Reported: Ollama configured but not in the fallback priority list, and a
request to add Hugging Face. Root causes found:

1. **The priority list overwrote the order with a stale copy.**
   `FallbackPriorityList` loaded `infra.llmProviders.order` once into
   private state. A card's "In fallback priority chain" switch updated the
   config, but the list never reloaded, and the next drag wrote its stale
   list back, silently dropping the provider. This is how `ollama` vanished
   from `config/tradingos.config.json`. The order is now owned by
   `LlmProvidersSection` and reloaded after every change. Saving a provider
   for the first time adds it to the chain, and removing it takes it out.
2. **`model: "auto"` was never translated.** Every real agent call (agent
   graph, CEO chat, strategy generation, suggestions) routes with
   `model="auto"`, and every client sent that literally, so each hosted
   provider was asked for a model named "auto". New
   `llm_router.resolve_model`: the provider's stored default model (new
   `default_model` on the LLM credential, set via
   `PUT /settings/llm-providers/{p}/default-model` or the card's
   Discover → Set default), else `HOSTED_DEFAULT_MODELS`, else that
   provider fails and the chain moves on. Ollama, Custom and Hugging Face
   have no built-in default and show a warning until one is set.
   `/test` with no `?model=` now tests exactly what "auto" would use.
3. **The router singleton never saw keys saved in Settings.** It built its
   clients once at first use. It now re-reads clients and default models
   from the store on each call (injected test clients unchanged).
4. **`PUT /gateway/config` returned 500 for valid JSON containing an
   escaped emoji.** `json5` decodes `"🧠"` (how Python's
   `json.dumps` writes the CEO agent's 🧠) into two lone surrogates, which
   Postgres rejects. `gateway/loader.py` now rejoins surrogate pairs, and an
   unpaired one is a 400 config error. The browser sends the emoji
   literally, so the UI switch didn't hit this, but any scripted/CLI client
   did.

**Hugging Face** is a new provider (`huggingface`): Inference Providers'
OpenAI-compatible API at `router.huggingface.co/v1` (chat completions and
`/models` discovery), key = an HF access token with the "Make calls to
Inference Providers" permission, `HUGGINGFACE_API_KEY` env fallback. Model
ids are HF repo ids; pick a default model after saving the token.

Verified live on the stack: Ollama default `qwen2.5:0.5b`, added to the
chain (`deepseek → anthropic → gemini → openai → ollama`). A real
`get_llm_router().complete(model="auto")` was served by DeepSeek (the
operator's key, `auto` → `deepseek-chat`). With Ollama pinned (chat's
per-session provider), it answered via `qwen2.5:0.5b`. `/test` with no
model passes for Ollama. Hugging Face is listed and awaiting a token.

**Top bar, second attempt.** The first fix looked like a flat, near-opaque
band because the blur never applied: declaring both `backdrop-filter` and
`-webkit-backdrop-filter` made the CSS pipeline keep only the `-webkit-`
form, which Chromium ignores. Now only the unprefixed property is written
(the pipeline adds the prefix itself, as it does for the mobile tab bar),
with a translucent gradient (74%→58% of `--background`), `blur(22px)
saturate(165%)`, a faint cyan hairline and a soft drop shadow, in keeping
with the original glass theme.

Tests: `test_llm_auto_model_and_huggingface.py` (17) and 3 new
`test_gateway_loader.py` cases (all 3 fail on the pre-fix loader,
reproducing the live `DataError`). `tsc --noEmit` clean.
