# TradingOS 2.0 — Project Memory (CLAUDE.md)

This file is read automatically by Claude Code at the start of every session in this repo. Keep it accurate as the build progresses — if a decision below changes, update this file in the same session, not later.

## What this project is

TradingOS is an AI multi-agent trading operating system for Indian equity & F&O markets (NSE/BSE). A 24-agent LLM organization, led by a CEO Agent, autonomously researches markets, generates and validates trading strategies, backtests them, and runs them in paper trading — all fully autonomous. **Real money is never moved without an explicit human approval per order.** This is a solo-operator project, self-hosted, not multi-tenant, not built for public/enterprise scale.

The full design is captured in two documents in `docs/` (read them before starting any phase you haven't touched yet):
- `docs/TradingOS_2.0_Blueprint.md` — feature research and OpenClaw-parity rationale (why each feature exists)
- `docs/TradingOS_2.0_Build_Specification.md` — the concrete architecture, schema, and module spec (what to build)
- `docs/TradingOS_2.0_Build_Prompt_Pack.md` — the phase-by-phase build prompts (how to build it, in order)

## Non-negotiable rules — never violate these regardless of what a feature request seems to imply

1. **No autonomous live-money orders.** The agent organization runs fully hands-off through paper trading. Any order that would place, modify, or cancel a position with real capital must pass through the `live_order_intents` human-approval queue (Build Spec §12). There is no code path where an agent or scheduled job submits a live order directly to a broker.
2. **The safety layer sits above the agents, never inside them.** Kill Switch, Compliance Checker, Naked-Options Scanner, Correlation Constraint, and the Go-Live Gate are deterministic Python, not LLM-judged, and no orchestration change may create a path around them.
3. **Heartbeat and any proactive/background agent behavior is read-only.** It may raise alerts and open tasks; it must have no reference to order-placement or risk-limit-mutation functions in its execution context — not just a permission check, an actual absence of the capability.
4. **No MFA.** Auth is password + JWT (15-min access / 7-day rotating refresh) + RBAC only. Don't add TOTP/MFA scaffolding even if a library makes it convenient.
5. **Skills are internal-only.** No dynamic loading of unreviewed/third-party code as an agent "skill." Every skill ships in this repo and is reviewed like any other code change.
6. **Strategy code execution is sandboxed.** LLM-generated strategy code never runs with raw filesystem/network/shell access outside the sandbox boundary (Build Spec §9).
7. **Every mutation is audited.** Any state-changing action — including Agent Gateway config changes — writes an append-only audit-log row. If you add a new mutating endpoint, add its audit entry in the same PR/commit, not later.
8. **Migrations are additive.** Never write a destructive schema change into `upgrade()`; it belongs in `downgrade()` only.

## Confirmed technical decisions

| Decision | Value |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn |
| Agent orchestration | LangGraph |
| Frontend | Next.js + TypeScript + Tailwind + shadcn/ui — **already built via v0.app**, lives in `frontend/`, currently running on mock data. Do not rebuild UI from scratch; wire it to real endpoints (Phase 13) |
| Primary DB | PostgreSQL 16 |
| Data lake | DuckDB + Parquet |
| Vector memory | Qdrant |
| Pub/sub | Redis 7 |
| Distributed compute | Temporal (single-node) |
| Secrets | Encrypted local secrets store (no Vault — dropped for solo-operator scale) |
| Deployment | Docker Compose, self-hosted, no Kubernetes |
| Sandbox | gVisor or Firecracker microVM for strategy code execution |
| Brokers | Zerodha Kite Connect (primary), Upstox (fallback) |

## Repository structure

See Build Specification §4 for the full layout. High points:
- `backend/src/gateway/` — Agent Gateway (config schema, loader, hot reload, CLI) — NEW, build first
- `backend/src/orchestration/` — planner, task engine, run manager, approvals, events
- `backend/src/agents/` — LangGraph nodes, personas, LLM router, skills
- `backend/src/engine/{backtest,optimization,risk,paper_trading,live,sandbox}/`
- `backend/src/brokers/`, `backend/src/data/`, `backend/src/memory/`, `backend/src/workers/`
- `backend/src/models/` — SQLAlchemy models (import every model in `__init__.py` — a missing import broke Alembic autogenerate in the prior build; don't repeat that)
- `config/tradingos.config.json` — the Agent Gateway config file
- `frontend/` — the v0-built Next.js console (mock data as of Phase 0; wired to real APIs in Phase 13)

## Working conventions

- Type hints everywhere; Pydantic models for all API request/response shapes and the Agent Gateway config schema.
- `pytest` for backend tests; a risk-critical module (Kill Switch, live-intent expiry, Compliance Checker) is not done until it has unit tests AND is on the mutation-testing list.
- Alembic for every schema change — no manual SQL against the dev DB.
- `ruff` + `black` (or `ruff format`) — run before considering a phase complete.
- Structured logging via `structlog` with a correlation ID threaded from HTTP request → orchestration run → agent action → order.
- When a phase's prompt (in the Build Prompt Pack) says to write tests for a specific behavior, write them — don't defer "we'll add tests later."
- If you find a spec ambiguity or something in the Build Specification that doesn't quite fit once you're actually writing code, stop and ask rather than silently deciding — flag it plainly (this is a solo project; there's no one else reviewing the diff).

## Current build status

Track progress here as phases complete — update this section at the end of every phase.

- [x] Phase 0 — Repo & Foundations — backend scaffold (§4 tree), FastAPI + async SQLAlchemy + Alembic baseline (`users`, `refresh_tokens`), JWT access/refresh auth with refresh-token reuse-detection, table-based RBAC policy engine (`src/core/rbac.py`) enforced via a single `require_role` dependency, 21 passing tests. Note: `docker compose build` could not be executed in this sandbox (Docker Hub pulls are blocked by the session's egress policy) — verified the equivalent behavior instead by running `alembic upgrade head` and the API directly against a local Postgres 16 and exercising register/login/refresh/reuse-detection/RBAC over HTTP; please confirm `docker compose up` once in a normal environment.
  - **Deviation from Build Spec §23 (by explicit request):** at the operator's request, Postgres and the API run as two supervisord-managed processes inside **one** Docker container (`TradingOS-2.0`, image `tradingos-2.0`), not as separate `api`/`postgres` containers. See `backend/Dockerfile`, `backend/supervisord.conf`, `backend/scripts/container-entrypoint.sh` (root ENTRYPOINT: bootstraps the Postgres data dir on first boot, then execs supervisord) and `backend/scripts/api-entrypoint.sh` (waits for Postgres, runs migrations, execs uvicorn). Every later phase that adds a Docker Compose service (qdrant, redis, temporal, minio, prometheus/grafana, frontend, etc.) needs a decision on whether it joins this same container or gets its own — don't silently default either way, ask.
  - **Host ports:** `docker-compose.yml`'s API port (`API_HOST_PORT`, default 8000) always publishes — it's the only way to reach the app. Postgres's host port (`POSTGRES_HOST_PORT`) is opt-in and unpublished by default: the API reaches Postgres over `localhost` inside the same container regardless, so port 5432 never needs to be exposed to the host, which is what makes `docker compose up` immune to the (very common) "port 5432 already allocated" conflict with a pre-existing local Postgres. Keep this pattern for any future port: don't publish a host port unless something outside the container genuinely needs it.
- [x] Phase 1 — Agent Gateway — config schema (`src/gateway/schema.py`, Pydantic, `extra="forbid"` everywhere, fixed 24-agent roster in `src/gateway/roster.py`), JSON5-tolerant loader (`src/gateway/loader.py`), hot-reload file watcher (`src/gateway/watcher.py`, watchdog, debounced by content hash, wired into `main.py`'s lifespan), the single validate→apply→DB-sync→audit pipeline (`src/gateway/apply.py`) used by startup, hot-reload, and the CLI alike, new tables `agent_identities`/`agent_bindings`/`agent_config_versions`/`agent_to_agent_policy` plus a minimal `audit_log` table, agent-to-agent policy enforcement (`src/orchestration/handoffs.py`, default-deny), service layer (`src/gateway/service.py`) and `tradingos-cli` (`src/gateway/cli.py`, `agents list/set-identity/bind/unbind/skills/heartbeat`, `config validate/rollback`, `doctor --fix`). 36 new tests (57 total). `config/tradingos.config.json` bind-mounted into the container (not baked into the image) so it stays hand-editable at runtime.
  - **`infra.riskThresholdRefs` is read-only**, enforced two ways: the Pydantic model is `frozen=True`, and no function anywhere in `src/gateway/` writes a risk-limit value — that only happens through the dual-control stage/confirm flow built in Phase 6.
  - **Known, accepted trade-off:** a CLI-driven config mutation rewrites the file as plain formatted JSON (via `json.dumps`), so it does not preserve hand-added JSON5 comments. Hand-edit if comments matter to you.
  - **Known, accepted trade-off:** if a CLI mutation runs against the same file a live app process is watching, both the CLI's own apply and the app's independent hot-reload detection will each write an `agent_config_versions` row for the same content — a harmless duplicate, not a correctness issue (verified live).
  - Verified live end-to-end against the real seed config and a running server: hand-edited the file mid-run (valid change applied within seconds, invalid change rejected with a clear error, app never crashed) and drove the identical changes via `tradingos-cli`.
  - **Default admin bootstrap (opt-in):** set `DEFAULT_ADMIN_EMAIL` + `DEFAULT_ADMIN_PASSWORD` in `.env` and `backend/scripts/api-entrypoint.sh` ensures that `SystemAdministrator` user exists on every boot (idempotent — creates it once, re-promotes/resets its password on later boots, never errors). Unset by default so a stock deployment never has a credential baked in; without it, the first user to register through the app becomes admin automatically (existing Phase 0 behavior). Verified live across a container restart.
- [ ] Phase 2 — Orchestration Engine
- [ ] Phase 3 — Agent Roster, LLM Router & Skill Registry
- [ ] Phase 4 — Strategy Pipeline & Sandbox
- [ ] Phase 5 — Backtesting & Optimization
- [ ] Phase 6 — Risk & Safety Layer
- [ ] Phase 7 — Paper Trading Engine
- [ ] Phase 8 — Broker Integrations
- [ ] Phase 9 — Live Order Intent Pipeline & Sign-off Queue
- [ ] Phase 10 — Market Data & Data Lake
- [ ] Phase 11 — Audit & Observability
- [ ] Phase 12 — Notifications & Omni-Channel
- [ ] Phase 13 — Frontend Integration (wire v0 UI to real backend)
- [ ] Phase 14 — Security & Testing Hardening Pass
