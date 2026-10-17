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
- `paper-trading-desk.spec.ts` -- the paper desk (`/paper-trading`) against the
  real backend. `backend/scripts/seed_e2e_paper_desk.py` writes what the engine
  would have left (positions, fills with costs, a switched-off subscription that
  still holds shares, a credit spread); nothing is priced, so the spec checks the
  desk says so. It also drives a close (refused for want of a real price, then
  cleared administratively), adding and switching a subscription off and on, and
  a RiskManager seeing the desk with no controls.

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
