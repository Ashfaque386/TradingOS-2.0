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
  or options-greeks field of any kind, and this codebase has no
  options-pricing model (e.g. a Black-Scholes solver) to compute one from
  LTP — so `call_iv`/`put_iv` are always `None` for this broker, not a
  temporary gap, never Upstox-parity. 10 new/updated backend tests
  (`backend/tests/test_brokers_zerodha.py`,
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

## Parts 1 and 4 — not attempted in this pass

Real broker OAuth, a real LLM provider/Ollama connection, a real
Telegram/Discord/Slack message actually arriving, and a live-NSE-hours
paper-trading smoke test all require two things this session genuinely
does not have: a real Docker stack (confirmed unrunnable here — `dockerd`
cannot start), and real developer-console credentials for external
services this sandbox's egress policy blocks regardless. Per this
project's own Development & Testing Workflow
(`docs/CLAUDE.md`), these were always meant to be exercised at
`D:\TradingOS-2.0\TradingOS-2.0`, not wherever the code was written — this
pass doesn't change that, it just means Parts 2/3's code is what's ready
to pull there. Once run locally, update this section (or the Phase 17
entry in `docs/CLAUDE.md`) with what was actually exercised, honestly,
the same way every other phase's live-verification paragraph in this
repo works.
