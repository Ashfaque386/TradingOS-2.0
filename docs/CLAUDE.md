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

- [x] Phase 0 — Repo & Foundations — backend scaffold (§4 tree), FastAPI + async SQLAlchemy + Alembic baseline (`users`, `refresh_tokens`), JWT access/refresh auth with refresh-token reuse-detection, table-based RBAC policy engine (`src/core/rbac.py`) enforced via a single `require_role` dependency, Docker Compose (`api`+`postgres`), 21 passing tests. Note: `docker compose build` could not be executed in this sandbox (Docker Hub pulls are blocked by the session's egress policy) — verified the equivalent behavior instead by running `alembic upgrade head` and the API directly against a local Postgres 16 and exercising register/login/refresh/reuse-detection/RBAC over HTTP; please confirm `docker compose up` once in a normal environment.
- [ ] Phase 1 — Agent Gateway
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
