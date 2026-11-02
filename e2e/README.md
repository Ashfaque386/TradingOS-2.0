# End-to-end Playwright suite

Build Spec §21-22 hardening pass: a real Playwright suite driving the
actual Next.js frontend against the actual FastAPI backend, real
Postgres, and real Redis -- no mocked network responses. It began with four
flows named in that pass and has grown since:

- `strategy-lifecycle.spec.ts` -- the full strategy pipeline (Ideation ->
  Backtesting -> Paper Trading -> Live Eligible), including both human
  sign-off decisions along the way.
- `live-autonomy.spec.ts` -- Phase 18's redesigned live-trading flow:
  enrolling a subscription, autonomy off producing no order at all,
  the confirmation-modal-gated master switch auto-resolving the next real
  signal with no per-order approval step, a tripped Kill Switch still
  blocking generation with autonomy on, and the standing rate cap
  stopping a runaway signal independent of the Kill Switch. Replaces the
  old `signoff-queue.spec.ts` (a human approving/rejecting a pending live
  order intent), which no longer applies -- see Non-Negotiable Rule #1.
- `kill-switch.spec.ts` -- a real drawdown tripping the paper kill
  switch, and a RiskManager/SystemAdministrator resetting it from the
  Overview console (PortfolioManager sees the tripped state but no reset
  control it isn't RBAC-permitted to use).
- `agent-gateway-hot-reload.spec.ts` -- hand-editing
  `config/tradingos.config.json` with intentionally invalid JSON5 and
  confirming the app rejects it and keeps running on the last-known-good
  config, exactly as that file's own top-of-file comment promises an
  operator.
- `session-resilience.spec.ts` -- a signed-in session surviving the session
  check failing with no response at all or answering 429/503 (no sign-out), an
  unreachable server showing a retry screen that recovers without a new login,
  and a genuinely rejected session still going to the login screen.
- `agent-pipeline-feed.spec.ts` -- the live per-step agent feed on Mission
  Control: a pipeline run started elsewhere appears in an already-open panel
  over the WebSocket (not the one-time history backfill), a run started from
  the panel shows its steps, and a role that cannot run the pipeline sees the
  feed but no run form.
- `agent-memory.spec.ts` -- the Agent memory panel. With no Qdrant server or
  embedding provider (CI's situation) it reports itself unavailable with the
  backend's own reason; an admin can save, reload and reset the embedding
  choice, which is stored in the database; only an admin sees that form. Three
  further tests -- add/search/delete a memory, the role limits, and a normal
  pipeline run leaving memory exactly as it found it -- run only when memory is
  actually online and are skipped otherwise (see "Memory-online tests" below).
- `pipeline-abandoned.spec.ts` -- a LangGraph pipeline run whose process died
  (the run lives inside one HTTP request, so a crash or redeploy leaves no
  ending). One run is planted *before the backend starts*, so the startup sweep
  is what ends it: the spec checks, through the API, that it now ends with a
  `pipeline.abandoned` event and that cancelling it is refused. A second,
  already-ended run is planted at test time so the Mission Control panel has an
  interrupted run to render: INTERRUPTED, the stuck step marked, no Cancel.
- `run-cancel.spec.ts` -- cancelling a paused organization run through the
  confirm step and checking each task's resulting status server-side, backing
  out with "Keep it", and a cancel that lost a race being reported rather
  than ignored.
- `paper-trading-logic.spec.ts` -- the paper desk's pure logic (cost waterfall,
  option payoff, expiry in IST, what needs attention). No browser or backend.
- `broker-quotes-logic.spec.ts` -- the wording of a lane's price section (the exchange stamp, which
  circuit band applies) and of a broker's refused-quote line. No browser or backend.
- `quote-freshness.spec.ts` -- a lane's Price section against the real backend, and the Providers tab
  and broker card reporting refused quotes. `backend/scripts/seed_e2e_quote_freshness.py` writes the
  ticks the tick source would have left (fresh with limits, quiet, unstamped, refused).
- `paged-lists.spec.ts` -- the runs on Mission Control, the strategies and the instrument master read a page at a time with the
  total (120 runs, 130 equities and 120 option contracts written by `scripts/seed_e2e_runs.py` and `seed_e2e_instruments.py`;
  the runs are dated in the past and removed again, so they never push another spec's runs off Mission Control's first page).
- `chat-settings-logic.spec.ts` -- a chat title's rule, the provider choices and their marks, the export file name, the Settings
  address. No browser or backend.
- `settings-sections.spec.ts` -- the Settings section in the address, and removing a broker's credentials or an LLM key asking
  first (the keys are stated by stubbed routes, so none is touched).
- `scheduled-jobs.spec.ts` -- a schedule edited in Mission Control saying it is kept, and the restore. (That a restart applies it is
  in the backend tests: this run cannot restart the app.)
- `gateway-logic.spec.ts` -- what a lost race says, which skills may still be granted, how a saved change and a narration are
  put in words. No browser or backend.
- `gateway-edits.spec.ts` -- edits of the gateway config in Agent Fleet and Settings against a real backend: a lost race in Agent
  Fleet, the LLM order and the raw editor (arranged by making the other editor's change when the page's request is sent), one
  version per save, who the audit names, skills chosen from the shipped set, the narration.
- `analysis-logic.spec.ts` -- the indicator parameters' bounds and wording, the Market Analysis address, the holiday wording, calendar
  days and India's date, the Markdown reader, the investor-report period rules, the Portfolio figures. No browser or backend.
- `analysis-pages.spec.ts` -- Market Analysis against a real backend: the address, the holiday calendar, the indicator periods, the
  lake's lag and the provider health (the last two stated by stubbed routes where the database cannot arrange them). It seeds a
  share's recent bars with `scripts/seed_e2e_recent_bars.py`.
- `strategies-logic.spec.ts` -- the line diff, the version and suggestion wording, the live limits form's rules, the live list's
  order. No browser or backend.
- `strategies-review.spec.ts` -- a strategy's own page against a real backend: its address and tabs, versions, the diff,
  suggestions, and the live limits (tightening, the position guards, switching off, who may). It seeds strategies with
  `scripts/seed_e2e_strategy_versions.py`, `seed_e2e_live_intent.py` and `seed_e2e_live_position.py`.
- `backtests-logic.spec.ts` -- what the Backtests page decides: the form rules, the requests, a run's description, walk-forward
  windows, the optimisation's wording, Monte Carlo rows, the ledger's summary. No browser or backend.
- `backtests-lake.spec.ts` -- backtests on the data lake against a real backend: a run from a share and two dates, its ledger,
  refusals, a walk-forward, the search for the best length, the Monte Carlo choices. It seeds a share with
  `scripts/seed_e2e_lake.py` (a new symbol each run).
- `paper-desk-history-logic.spec.ts` -- what the Paper Desk says about fetching price history, daily signals and the fills
  file, the order window, and how the desk is read when part of it fails. No browser or backend.
- `paper-desk-history.spec.ts` -- the Paper Desk's history against a real backend: the fetch offered and refused, the
  signals tab, orders opened eight at a time, the CSV export, a figure that cannot be read. It seeds a subscription
  with `scripts/seed_e2e_paper_history.py` (a new symbol each run).
- `orders-logic.spec.ts` -- what the Orders & Trades page decides: a tone for every status, what a price rests on, the
  request for each filter and page, positions and profit formatting. No browser or backend.
- `orders.spec.ts` -- the Orders & Trades page against a real backend: the ledger paged to the last row, server-side
  filters, a price's source, audit-trail links by role, a strategy's live orders and trades, the intents. It seeds a
  strategy with `scripts/seed_e2e_orders.py` (new names each run).
- `audit-logic.spec.ts` -- what the Audit page decides: India time to UTC, the request and the address for each filter,
  page windows, the wording of a verification, an actor's action shares. No browser or backend.
- `audit.spec.ts` -- the Audit Log against a real backend: paging to the last entry, server-side filters, a link that
  opens it narrowed, an entity's trace, an actor's summary, filtered exports, the verification result. It signs in many
  times and creates an account to have entries of its own to look for.
- `csp-logic.spec.ts`, `users-logic.spec.ts` -- the Content-Security-Policy builder and the wording of Settings > Users.
  No browser or backend.
- `access-control.spec.ts` -- closed registration, accounts made by an administrator, the lock after wrong passwords and
  its unlock, WebSocket tickets, the API headers and the absent docs. It creates accounts with unique emails.
- `csp.spec.ts` -- every page loaded under the Content-Security-Policy with a listener for violations.
- `backups-logic.spec.ts` -- the wording of Settings > System and backups (sizes, ages, the one line per
  kind of backup, what a row holds). No browser or backend.
- `backups.spec.ts` -- the same section against the real backend: nothing backed up yet, **Back up now** making
  a real `pg_dump` and manifest (so `pg_dump` must be installed, as CI's `postgresql-client-16` step does), an
  unfinished dump shown as not usable, and the section refused to other roles. It empties `backend/backups`
  first and afterwards.
- `engine-alerts-logic.spec.ts` -- the wording of the engine-alert panel, the top-bar
  count and Settings' Alertmanager line. No browser or backend.
- `engine-alerts.spec.ts` -- what the trading engine needed a person for: the
  panel on the Paper Desk (open and cleared alerts, the link to the subscription
  an alert is about), the count in the top bar that leads to it, an auditor reading
  it, and Settings' line about the path from Prometheus to a channel.
  `backend/scripts/seed_e2e_engine_alerts.py` writes the episodes the watcher, the
  engine and Alertmanager would have left. The paper-trading logic spec also runs
  `e2e/fixtures/paper-attention.json`, the cases the server's own copy of the
  attention rules runs (`backend/tests/test_paper_attention.py`).
- `paper-trading-desk.spec.ts` -- the paper desk (`/paper-trading`) against the
  real backend. `backend/scripts/seed_e2e_paper_desk.py` writes what the engine
  would have left (positions, fills with costs, a switched-off subscription that
  still holds shares, a credit spread); nothing is priced, so the spec checks the
  desk says so. It also drives a close (the estimate says there is no price, the
  button waits for the typed confirmation, then an administrative close clears
  it), adding a subscription with the symbol search saying no broker is
  connected, switching it off and on, editing a subscription's settings, and a
  RiskManager seeing the desk with no controls.

## Why not `docker compose up`

The suite runs against services started directly (below), not through
`docker compose up`. It was first written in a sandbox with no reachable Docker
daemon (the same constraint `src/engine/sandbox/factory.py`'s gVisor fallback
documents elsewhere); CI's `e2e` job (`.github/workflows/ci.yml`) starts the
same services directly on the runner, and doing it by hand needs no image
build.

## Running locally

You need, running locally (not via Docker):

1. **Postgres** with a `tradingos` database and a `tradingos` role
   (`postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos`),
   migrated to head:
   ```
   cd backend && DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
     .venv/bin/python -m alembic upgrade head
   ```
2. **Redis** on the default port (`redis-server`).
3. **The backend**, on port 8000 (`live-autonomy.spec.ts` saves its own
   fixture Zerodha credentials via the real API mid-test, never real ones
   -- see `backend/scripts/seed_e2e_users.py`'s own docstring for why this
   pattern is fixture setup, not a production credential-bootstrap path):
   ```
   cd backend
   DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
   JWT_SECRET_KEY=dev-only-secret-do-not-use-in-production \
   CORS_ORIGINS=http://localhost:3010 \
   AGENT_GATEWAY_CONFIG_PATH=../config/tradingos.config.json \
   NSE_HOLIDAYS_PATH=../config/nse_holidays.json \
   SECRETS_ENCRYPTION_KEY=<output of: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"> \
   SECRETS_STORE_PATH=secrets/e2e_broker_credentials.enc \
   API_RATE_LIMIT_PER_MINUTE=100000 \
   PAPER_TRADING_SYNTHETIC=true \
     .venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
   ```
   `PAPER_TRADING_SYNTHETIC=true` is test-only: paper and live trading normally refuse to run
   on generated prices or history for a symbol, and these specs use demo symbols.
4. **The frontend**, on port 3010 (matches `playwright.config.ts`'s
   default `baseURL`; override with `E2E_BASE_URL` if you use a different
   port):
   ```
   PORT=3010 pnpm dev
   ```
5. **Fixture users**, once:
   ```
   cd backend && DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
     JWT_SECRET_KEY=dev-only-secret-do-not-use-in-production \
     .venv/bin/python scripts/seed_e2e_users.py
   ```
Then, from the repo root:

```
npx playwright test
```

`backend/scripts/seed_e2e_live_intent.py` is called by `live-autonomy.spec.ts`
itself (via `e2e/utils.ts`'s `runSeedScript`), not something you run by
hand -- it seeds a fresh LiveEligible strategy per test; the spec itself
enrolls it into live trading, flips its autonomy switch, and saves its own
fixture Zerodha credentials through the real API.

`backend/scripts/seed_e2e_pipeline_run.py` plants the dead pipeline run for
`pipeline-abandoned.spec.ts`. Its default mode must run **before the backend
starts** (the e2e job in `ci.yml` does, with a fixed run id the spec knows --
add the same line before step 3 when running by hand), because the backend's
startup sweep is what the first test exercises; the spec itself calls it with
`--abandoned` for the panel test.

`backend/scripts/seed_e2e_paused_run.py` is called the same way by
`run-cancel.spec.ts`. It plants a PAUSED organization run (with a finished, a
ready and a blocked task) because there is no other way to get one: every
capability in the organization-run engine is an instant stub, so a run created
through the API is already completed when the request returns. The spec then
drives the cancel flow through the real UI and API and checks each task's
resulting status server-side. Each call seeds a fresh, uniquely-named run, so
the spec can be re-run against the same database.

## Memory-online tests

Three tests in `agent-memory.spec.ts` -- add/search/delete a memory, the role
limits, and a normal pipeline run recording nothing -- run only when memory is
actually online, and skip otherwise. CI's
stack has no Qdrant server and no embedding provider, so CI never runs them;
run them by hand. Start a Qdrant server, make an embedding provider reachable,
and add these to the backend's environment (step 3 above):

```
QDRANT_URL=http://localhost:6333
MEMORY_EMBEDDING_PROVIDER=ollama
MEMORY_EMBEDDING_MODEL=nomic-embed-text      # ollama pull nomic-embed-text first
OLLAMA_BASE_URL=http://localhost:11434       # wherever that Ollama is reachable from the backend
```

e.g. `docker run -d -p 6333:6333 qdrant/qdrant:v1.19.1`. Use a **throwaway**
Qdrant: `agent-memory.spec.ts` writes (and deletes) test memories in whatever
Qdrant the backend points at. With memory online the pipeline specs also read
from it (they recall lessons) but a normal run records nothing -- only generated
code that fails validation is ever remembered -- so they leave it as they found it.
(An embedding choice saved from the panel overrides these two variables --
`agent-memory.spec.ts` saves and resets one -- so run against a database that
has none saved if you want the environment values to be what's tested.)
