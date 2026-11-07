# TradingOS 2.0

An AI multi-agent trading operating system for Indian equity and F&O markets (NSE/BSE). A 30-agent
LLM organisation researches markets, writes and validates strategies, backtests them and runs them in
paper trading. Live capital runs autonomously once a strategy has earned live eligibility and its own
master switch is on; from then on only the deterministic safety layer (kill switch, compliance,
naked-options scan, correlation constraint, per-strategy caps) can stop an order. It is a
self-hosted, single-operator system, not a multi-tenant product.

> **Status.** Everything below is built, tested and running, but it has not yet been through a full
> live trading session with real brokers. Read [`docs/gap-analysis-2026-10-30.md`](docs/gap-analysis-2026-10-30.md)
> before connecting real capital: it lists what is still missing, ranked, with the file that shows each gap.

## What is in it

| Area | What it does |
|---|---|
| **Agent organisation** | 30 agents under one gateway config (`config/tradingos.config.json`, hot-reloaded, versioned, audited). Orchestration engine, LangGraph strategy pipeline, a multi-provider LLM router, long-term vector memory (Qdrant). |
| **Strategies and backtests** | Objective → generated strategy → AST ban-list and lint → process-isolated sandbox → backtest with the Indian cost model, walk-forward, Monte Carlo, Optuna sweeps. |
| **Paper trading** | Runs on **real broker quotes and 5-level depth only**; with no real feed a subscription halts and says why, it never trades generated prices. Equities, monthly futures (lots, margin estimate, roll) and defined-risk option spreads (per-leg positions, expiry square-off), charged brokerage, STT, exchange fees, SEBI fee, stamp duty and GST per fill. |
| **The Paper Desk** (`/paper-trading`) | Today's result as *price result − costs = net*, what needs attention, one lane per subscription (legs, payoff at expiry, costs and fills, settings), add / switch off / close / edit dialogs with live previews and typed confirmation. |
| **Live trading** | Order-intent pipeline with the safety layer in front of the broker, per-strategy autonomy switch, standing rate and notional caps, Zerodha Kite and Upstox adapters. |
| **Safety and audit** | Kill switch, compliance checker, naked-options scanner, correlation constraint, dual-control risk limits, a hash-chained append-only audit log with a WORM archive. |
| **Operations** | Prometheus + Alertmanager + Grafana (tick age, quote-call latency, order dispatch), notifications to Telegram / Discord / Slack, **engine alerts** (a position nobody is watching, a stopped price feed, a stop-loss exit, a roll or square-off that could not be done, a broker refusing its credentials; listed on the Paper Desk, counted in the top bar), scheduled market-data ingestion into a DuckDB/Parquet lake, nightly lake backup. |

## Running it

Docker is the supported way. `docker compose up` builds and runs the frontend, the backend (API and
its Postgres in one container), Redis, Qdrant, Temporal, Prometheus, Alertmanager and Grafana.

```bash
cp .env.example .env        # then set SECRETS_ENCRYPTION_KEY, see below
docker compose up --build -d
```

Ports (all bound to `127.0.0.1` unless you set `BIND_HOST`): frontend **3000**, API **8000**,
Prometheus **9090**, Grafana **3001** (Alertmanager has no host port: Prometheus and the backend reach
it over the stack's network). Open <http://localhost:3000> and register: **the first user to
register becomes the SystemAdministrator**, so do that before exposing the port to anyone else. Registration is closed from then on; the administrator makes other accounts under Settings > Users.

**First-time setup — Settings UI (Broker Config, LLM Providers, Notification Channels):** these three
panels share one encrypted store and fail with a 503 until you set `SECRETS_ENCRYPTION_KEY` in `.env`:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep that key somewhere safe: it is the only way to read the stored broker and LLM credentials. The
backend logs a startup warning, and does not fail, if it is unset. Everything else, including paper
trading on generated data (`PAPER_TRADING_SYNTHETIC=true`), works without it.

**Connecting brokers.** Settings → Broker Config. Zerodha and Upstox access tokens end around
**06:00 IST every day**, so each trading morning you sign the brokers in again (a reminder is sent to
your notification channels). Paper trading needs a connected broker for quotes, depth, history and
the instrument lists; without one the Paper Desk's symbol search and previews say so and subscriptions
wait.

**Alerts.** Settings → Notification Channels has five alert levels per channel. *Engine alerts* tells you when the trading engine needs you: a switched-off subscription still holding a position, a price feed that has stopped, a position with no price, a stop-loss exit, a futures roll or an expiry square-off that could not be done, a broker refusing the stored credentials. A condition is announced once when it starts (a warning must last `ENGINE_ALERT_CONFIRM_SECONDS`, two minutes by default; a position nobody is watching is announced at once) and once when it clears. Every alert, open or cleared, is listed on the Paper Desk and counted in the top bar. Prometheus's rules reach the same channels through Alertmanager, which posts to the backend with a token created on first boot; Settings shows whether that path is up. One limit: an alert about the backend being down cannot be delivered by the backend.

**Stale quotes.** A broker answers a quote request even when the exchange is shut, with the last price it has and the exchange's own timestamp for it. A quote stamped on an earlier day is refused (it is counted, shown on the lane and in Settings, and nothing trades on it); `PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS` can also refuse a quote older than that many seconds, and is off until you have seen a real session's lag. Orders are checked against the exchange's own circuit limits when the broker's quote carries them, and otherwise against a reference-table band, which the order's reason says. A stop-loss exit is not held to the circuit band.

**Tuning.** Every `PAPER_*` setting (tick and order-book age limits, futures margin and roll, option
expiry rules, cost overrides) is in `.env.example`, forwarded by `docker-compose.yml`, and explained in
`backend/src/core/config.py`.

If port 8000 or 3000 is taken, use `pnpm docker:up` instead of `docker compose up`: it walks forward to
the next free port for whichever one is busy and passes extra flags through (`pnpm docker:up -d`).
Without Node installed, `.\scripts\docker-up.cmd -d --build` does the same on Windows. Behind a
TLS-intercepting antivirus or proxy, put its root certificate at `.local/extra-ca.pem` (git-ignored);
both launchers pass it to the image build and the running backend.

### Published images (GHCR)

CI builds both images on every run and publishes them from `main` (`latest`, `sha-<short>`) and from
`v*` tags:

- `ghcr.io/ashfaque386/tradingos-2.0-allinone`: frontend, backend, Postgres, Redis and nginx behind one port (8080), host-agnostic.
- `ghcr.io/ashfaque386/tradingos-2.0-backend`: the backend with its embedded Postgres, as `docker-compose.yml` runs it.

The frontend-only image is not published: Next.js inlines `NEXT_PUBLIC_API_URL` at build time. Build it
with the URL you need (`docker compose build frontend`).

```bash
docker run -d -p 8080:8080 --security-opt seccomp=deploy/seccomp/backend.json \
  -v tradingos_secrets:/app/secrets -v tradingos_data_lake:/app/data_lake \
  -v tradingos_data_lake_backups:/app/data_lake_backups -v tradingos_audit_archive:/app/audit_archive \
  -v tradingos_backups:/app/backups \
  -e SECRETS_ENCRYPTION_KEY=<fernet key> -e JWT_SECRET_KEY=<long random string> \
  ghcr.io/ashfaque386/tradingos-2.0-allinone:latest
```

The seccomp profile is required for strategy creation (the sandbox needs `unshare`).

**What lives where.** Everything the app writes is on a named volume, so `docker compose down && docker compose up --build`
keeps it: `tradingos-2.0_pgdata` (the database), `…_secrets` (the encrypted broker, LLM and channel stores; useless without
`SECRETS_ENCRYPTION_KEY`), `…_data_lake`, `…_data_lake_backups`, `…_audit_archive` and `…_backups` (the nightly Postgres dumps, see below).
The data lake, its backups and the audit archive were inside the
container until recently, so every rebuild deleted them. **Upgrading an existing stack:** before the first rebuild that
includes the volumes, copy them out of the running container, then copy them back in afterwards:

```bash
for d in data_lake data_lake_backups audit_archive; do docker cp TradingOS-2.0-Backend:/app/$d ./$d.keep; done
docker compose down && docker compose up --build -d
for d in data_lake data_lake_backups audit_archive; do docker cp ./$d.keep/. TradingOS-2.0-Backend:/app/$d; done
```

**Sign-in and sessions.** Only the first account can register; an administrator creates the others (Settings > Users). Five wrong passwords within 15 minutes lock that account for 15 (an administrator can unlock it; `LOGIN_MAX_FAILED_ATTEMPTS`, `LOGIN_FAILURE_WINDOW_MINUTES`, `LOGIN_LOCKOUT_MINUTES`). The console keeps its access token in memory and the refresh token in an HttpOnly cookie, so reloading a page restores the session and nothing a script can read is stored; a curl client needs a cookie jar (`-c`/`-b`) for `/auth/refresh`. WebSockets open with a one-time ticket from `POST /auth/ws-ticket`. The API's `/docs` is off unless `API_DOCS_ENABLED=true`. The stack serves plain HTTP on loopback: **put a TLS-terminating proxy in front before exposing it**, then set `TRUSTED_PROXIES` to that proxy's address (so rate limits see real client addresses and the cookie is marked Secure), `HSTS_ENABLED=true`, and send HSTS from the proxy for the console. The API and the all-in-one image's frontend run as an unprivileged user; the first start after upgrading hands the data volumes to it, which can take a moment for a large data lake. Rebuilding signs everyone out once.

**Backups.** The database is dumped every night at 22:45 IST to the `tradingos-2.0_backups` volume (the newest 7, and any younger than 14 days, are kept), each with a manifest and a checksum, and Settings > System and backups shows them and can take one now. A failed night raises an alert. `python -m scripts.restore_postgres list|verify|restore` (run with `docker exec TradingOS-2.0-Backend`) puts one back and checks the result against its manifest. A backup does **not** contain `SECRETS_ENCRYPTION_KEY` (without it the saved broker, LLM and notification credentials cannot be read, so keep it somewhere other than this machine), and the dump holds the login signing keys in plain text. It is on the same machine as the database: copy the newest one elsewhere. See [docs/runbooks/postgres-restore.md](docs/runbooks/postgres-restore.md).

## Developing and testing

```bash
pnpm install && pnpm dev                      # the console, on :3000, against a backend on :8000
pnpm exec tsc --noEmit                        # type-check (next build does not: ignoreBuildErrors)
cd backend && pytest -q                       # ~2,300 tests; needs Postgres and Redis (see CI for the env)
cd backend && ruff check src tests && ruff format --check src tests && mypy src && bandit -c pyproject.toml -r src
pnpm test:e2e                                 # Playwright against a real backend; see e2e/README.md
```

CI (`.github/workflows/ci.yml`) runs only what a change can affect: a secrets scan, ruff, bandit,
`mypy --strict`, pip-audit, pytest with global and per-module coverage floors, mutation testing of the
safety layer, the Playwright suite, `tsc`, and the image builds. Most backend tests are written against
real Postgres and Redis, not mocks.

**Time-dependent tests.** A test that writes data on a fixed date and reads a window from "today" stops
passing when the calendar moves on (one did on 29 Oct 2026). Place such data relative to today, or
inject a clock.

## Where things are written down

- [`docs/CLAUDE.md`](docs/CLAUDE.md): the project memory: rules that never bend, decisions, and a dated
  entry for every phase and follow-up, with what was verified live and what was not. **Start here.**
- [`docs/gap-analysis-2026-10-30.md`](docs/gap-analysis-2026-10-30.md): what is still pending and where the gaps are.
- [`docs/runbooks/first-session.md`](docs/runbooks/first-session.md): the first session on real data (brokers, the paper-trading numbers to read, the open data questions); [`docs/runbooks/postgres-restore.md`](docs/runbooks/postgres-restore.md): backups and restore.
- `docs/TradingOS_2.0_Build_Specification.md`, `…_Blueprint.md`, `…_Build_Prompt_Pack.md`: the design and build plan.
- `docs/phase16-wiring-audit.md`, `phase19-audit.md`, `phase20-*.md`: the audits and the old-app comparison backlog.
- [`e2e/README.md`](e2e/README.md): the end-to-end suite and how to run it.

## The frontend

The console is a Next.js app at the repository root (`app/`, `components/`, `lib/`) that was started
in [v0](https://v0.app) and is now wired to the real backend; it is edited in this repository.
Design tokens and the shared glass-panel styles are in `app/globals.css`; `lib/api.ts` is the one
place that talks to the backend, and `lib/paper-trading.ts` holds the Paper Desk's pure logic.
