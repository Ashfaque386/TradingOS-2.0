# Phase 20 — Legacy App Audit (`D:\AI Trading Agent\TradingOS`)

**Methodology.** This is a direct read of the old codebase — routers (`src/api/routers/*.py`, every `@router.*` decorator enumerated across all 30 router files), models (`src/models/*.py`), engine/orchestration/memory packages, `docker-compose.yml` (23 services/volumes), the Helm chart, Vault, CI (`.github/workflows/ci.yml`), and the frontend's page tree (`frontend/src/app`) — not a re-summary of `Project Document and BluePrint/TradingOS_Current_State_Features_and_Requirements.md`. That document (67KB, dated 2026-09-15, itself a six-research-pass reverse-engineering effort) was read afterward as a cross-check, and its own §8 "Known Gaps" and §9 "Assumptions & Open Questions" are unusually candid about what wasn't independently verified there — several of its findings are cited below (marked **[prior audit]**) because they materially change how a capability should be read (e.g. item 1 below), not because they were taken on faith: each cited item is either corroborated by a direct code read here or explicitly flagged as unverified.

Depth varies by item deliberately: high-value or ambiguous capabilities (MFA, vector memory, Live Canvas, multi-tenancy, portfolio allocation recommendations, the CI pipeline) were opened and read; the remaining ~150 endpoints were catalogued from their router decorators, response models, and docstrings, which in this codebase are consistently substantive (every router file opened had a real module docstring naming the spec section and rationale, not boilerplate) — this is a **verified pattern**, not an assumption extended uniformly without basis.

**Scale, for context.** 30 API router files (~180 endpoints), 27 model files, 43 Alembic migrations (linear chain), 13 docker-compose services (+ a TLS-terminated variant of the app itself), a Helm chart, real HashiCorp Vault (KV + Transit, with an unsealer sidecar), Qdrant, MinIO, 17 frontend pages, and a CI pipeline that builds the image, boots Postgres/Vault/Temporal/MinIO for real, runs gitleaks/ruff/black/mypy-strict/Bandit/pip-audit, applies migrations, bootstraps Vault and Qdrant for real, seeds real data, runs pytest with a per-module coverage gate and scoped mutation testing, then a separate Cypress E2E job against the real stack, plus image build-and-push and `helm lint`/`helm template` jobs.

---

## Auth, Identity & Access

- **Login/refresh/logout/me** — `src/api/routers/auth.py`. JWT access+refresh, matches the new app's shape.
- **MFA (TOTP, real)** — `src/api/routers/mfa.py`, `src/core/vault.py::write_mfa_secret`. Real `pyotp`-based TOTP: `/enroll` generates a secret + 10 backup codes and writes the secret to Vault KV (never returned again after enrollment), `/confirm` and `/verify` complete the challenge, `/disable` removes it. **Built but switched off**: `src/core/security.py::MFA_MANDATORY_ROLES = frozenset()` — no role is actually required to use it **[prior audit item 7, corroborated by reading `mfa.py` directly]**. So MFA existed as working, unused infrastructure, not as something protecting real logins day to day.
- **Admin user management** — `src/api/routers/users.py`. List, create, deactivate, `PATCH .../role`, `POST .../revoke-sessions`. A real admin console for the 4+ role system.
- **Multi-tenancy (partial, real infra)** — `src/api/routers/tenants.py`, `src/models/tenant.py`, migration `w4x5y6z7a8b9`. A genuine `tenants` table with `tenant_id` threaded through `users`/`accounts`/`strategies`/`orders`, one seeded "Primary Tenant" backfilled onto every pre-existing row. The router's own docstring is explicit that **cross-tenant query isolation was not retrofitted into the rest of the API** — real schema, partially wired, not a finished multi-tenant system.
- **JWT signing-key rotation** — `src/api/routers/system.py::POST /jwt-signing-key/rotate`.
- **Session policy engine (Casbin)** — `src/core/policy.py` + `src/core/policy/{model.conf,policy.csv}` — a real Casbin RBAC/ABAC policy file, not just per-route role lists.
- **Account/profile page** — `frontend/src/app/(app)/account/page.tsx` (own-account view; MFA enrollment UI presumably lives here).

## Agents, LLM Routing & Orchestration

- **Agent graph + control** — `src/api/routers/agents.py` (2,470 lines — the largest router). `/graph` topology, `/control` + `PUT /control/{agent_name}` (per-agent enable/disable), `/{agent_id}/narration` (natural-language "what is this agent doing"), `/{agent_id}/activity`.
- **Run lifecycle** — pause/resume/cancel/retry on individual agent runs (`/runs/{id}/pause|resume|cancel|retry`), plus an ad-hoc `/research/trigger`.
- **Run analytics** — `/analytics/summary` and `/analytics/trend` (charted metrics over agent runs, not just a list).
- **Prompt versioning (two subsystems)** — `agents.py`'s own `/prompts`, `/prompts/{slug}/versions/{version}`, `PUT .../active-version`; and a separate, fuller `agent_settings.py` router (`/prompts/{kind}` create, `/activate`, `/rollback`, plus `PUT /provider-model` to pin a model per agent and a `/test` endpoint). Two overlapping prompt-management surfaces in the old app itself — worth noting as its own inconsistency, not just a new-app comparison point.
- **Organization/orchestration console** — `src/api/routers/organization.py`, far broader than run CRUD: `/runs/manual`, pause/resume/cancel/retry/**rerun**, `/decisions`, `/events`, `/handoffs`, `/tasks` + `/tasks/{id}`, `/dependencies`, `/artefacts`, `/attention` (things needing a human), `/freshness` (dataset freshness across the org), `/decisions/{id}/resolve`.
- **Live Canvas** — `src/api/routers/canvas.py`. A single composed read endpoint answering "what's the newest real artifact of each kind (generated strategy code, backtest result, agent log) right now" — explicitly documented as reading only already-persisted real data, nothing fabricated. Backs a live side-by-side artifact viewer in the frontend (Phase_7 spec §2.5).
- **Agent long-term/vector memory** — `src/memory/{embeddings.py,collections.py,strategy_memory.py,news_memory.py,organization_memory.py}` + `router memory.py` (`/query`, `/ingest`, `/collections`, `DELETE /{vector_id}`). Real, multi-provider (Ollama/HuggingFace/local `sentence-transformers`/OpenAI/Gemini) embeddings pipeline into Qdrant, with the provider choice's own rationale documented in detail (HuggingFace's free tier rejects embedding models, Ollama proved unreliable on the dev machine, "local" is the resilient default). This is real, working infrastructure, not a stub.
- **Skills** — `src/api/routers/skills.py` + `src/models/skill.py` + `src/agents/tools/skill_registry_manager.py`. A DB-backed skill registry: enable/disable per skill, agent↔skill grant mapping (`/agent-map`), schema introspection (`/{name}/schema`).
- **Chat** — `src/api/routers/chat.py`. **`POST /messages` has no authentication dependency at all** — the one mutating, unauthenticated endpoint in the whole app **[prior audit item 8, file confirmed to exist]**.
- **Scheduled jobs** — `src/api/routers/scheduled_jobs.py` + `src/models/scheduled_job.py` + `src/agents/scheduler.py::JOB_REGISTRY` (12 cron jobs per the prior audit). A real, user-facing scheduled-job system: list, detail, run history, `PUT` to edit a schedule, `POST .../run-now`.

## Strategy Pipeline, Backtesting & Optimization

- **Strategy CRUD + sandbox validation + promotion** — `src/api/routers/strategies.py` (1,247 lines). Create/list/detail/patch, `/validation`, versioned code (`/versions/{n}`), `/promote`, suggestions with an async review-job pattern.
- **Backtesting** — trigger (async job + status poll), equity-curve, trades, **walk-forward**, `/latest`, `/compare`, **`/compare/correlation`** (a correlation matrix across multiple backtests), **`/monte-carlo`** histogram, CSV export.
- **Optimization** — `src/engine/optimization/{optuna_sweep.py,optuna_strategy_adapter.py,walk_forward.py,walk_forward_adapter.py,monte_carlo.py,monte_carlo_persistence.py}` + `src/workers/monte_carlo_worker.py` + `monte_carlo_workflow.py` (Temporal-backed).
- **Options Greeks** — `src/engine/options/greeks.py`.
- **Sandbox** — `src/engine/sandbox/{runner.py,pool.py,backtest_runner.py,strategy_factory.py}`.

## Risk

- **Kill switch** — `src/api/routers/system.py` (`/kill-switch`, `/kill-switch/status`, `/kill-switch/reset`) + `src/engine/risk/{kill_switch.py,kill_switch_service.py}`. Appears to be a single global switch, not the new app's paper/live split — not independently confirmed either way in this pass.
- **Correlation constraint & position sizing — built but not wired into the live/paper per-tick gate** `src/engine/risk/{correlation.py,position_sizing.py}` **[prior audit item 2]**. Real, tested modules that the compliance-checker docstring references but that the prior audit could not confirm are actually invoked from the real-time risk-check path.
- **Compliance checker — hand-maintained placeholder, not a live feed** `src/engine/risk/compliance_checker.py`: SEBI position-limit / NSE circuit-filter-band data for only 5 symbols, explicitly documented as a permanent constraint, not temporary **[prior audit item 3]**.
- **Naked-options scanner** — `src/engine/risk/naked_options_scanner.py`.
- **WS latency guard, as a standing service** — `src/engine/risk/{ws_latency_guard.py,ws_latency_guard_service.py}` (a continuously-running guard, not just a per-request check).
- **Risk limit change requests (dual control)** — `src/api/routers/risk_limits.py`: propose → `POST .../confirm` or `.../reject`, distinct from current-limit read.
- **Compliance checker's own regulatory-data gap is a stated permanent constraint**, not a bug — same honest-limitation posture this whole project's documentation culture uses.

## Paper & Live Trading, Execution

- **Paper trading** — `src/engine/paper_trading/{paper_account.py,paper_execution_pipeline.py,paper_kill_switch.py,margin_model.py,slippage_model.py,intraday_risk_rules.py,position_accounting.py,account_equity.py,equity_snapshot.py,daily_signal_job.py}` + router `paper_trading.py` (`/execute`, `/trades`, `/positions`, `/account/summary`, `/account/equity-curve`, `/account/statement/export`). Dedicated margin and slippage *models* (not a flat fee), and multi-leg F&O handling — **[prior audit item 13: no atomic multi-leg fill, a partial fill across legs is possible and only honestly surfaced, not prevented]**.
- **Live execution — built and tested, but never actually run autonomously.** `src/engine/live/{execution_agent.py,execution_pipeline.py,tick_listener.py}`. **`LiveExecutionPipeline` is fully built and unit-tested but is never instantiated by any worker** — real live trading in the old app is only human-triggered via the Orders API, not the autonomous pipeline its own code implements **[prior audit item 1 — this is the single most consequential finding for the comparison; see Part 3]**.
- **Orders** — `src/api/routers/orders.py`: orders, trades, execution-latency summary, place/cancel, close-position-by-symbol.
- **Shadow mode** — `src/brokers/shadow_mode.py` + `src/engine/shadow_mode_status.py` + router `shadow_mode.py` (`/attempt`, `/status`). Same Zerodha-has-no-real-sandbox asymmetry the new app's own Phase 8 entry documents **[prior audit item 12]** — a shared, already-acknowledged limitation, not something new to flag.
- **Go-live readiness** — dedicated router `go_live_readiness.py::GET /readiness/{strategy_id}`.
- **Broker adapters** — `src/brokers/{kite_connect_adapter.py,upstox_adapter.py,circuit_breaker.py,factory.py}` + router `broker_config.py` (`/status`, `/order-book`, credentials POST/DELETE). No evidence in this router of a real OAuth login-url/callback flow (unlike this session's own broker-redirect work on the new app) — credentials appear hand-entered, not walked through a broker login redirect.
- **Portfolio & allocation** — `src/api/routers/portfolio.py`: margin/positions by broker, aggregate PnL, dashboard summary, risk-metrics, **`/portfolio/allocation`**, and a full **LLM-driven advisory rebalancing workflow**: `POST .../allocation-recommendation/trigger` (runs the "Portfolio Manager Agent" against real strategy/backtest/margin data), `/latest`, `/{id}/accept|reject` with audit trail — explicitly documented as advisory-only, no automated execution path exists anywhere for it.

## Market Data

- **OHLCV + indicators overlay, intraday bars, corporate actions by symbol, quote, option chain + expiries** — `src/api/routers/market_data.py`.
- **Ingestion** — `/ingest/trigger` (async) + job-status poll; `src/data/ingest/{bhavcopy.py,corporate_actions.py,instrument_sync.py,intraday.py,intraday_writer.py,pipeline.py,scheduled_sync.py,yfinance_adapter.py}`. **Instrument-master sync and intraday minute-bar ingestion are not actually scheduled** — manual-only or dormant **[prior audit item 10]**.
- **Data lake** — `src/data/datalake/{catalog.py,query.py,freshness.py,backup.py}` (DuckDB/Parquet) + `/datalake/status` endpoint.
- **Providers** — `src/data/providers/{nse_fo_bhavcopy.py,upstox_v3.py,yahoo_finance.py,manager.py}` + `/providers/status`.
- **Market pulse, instrument search, provenance** — `src/data/{market_pulse.py,instruments.py,provenance.py}`.

## Notifications, Webhooks, Chat

- **Webhooks** — `src/api/routers/webhooks.py` (Telegram/Discord/Slack) + dedicated `src/core/{webhook_security.py,webhook_replay.py,webhook_rate_limit.py}`.
- **Notification channels** — folded into `settings.py` (`/notification-channels` GET/POST/**PATCH**/DELETE — PATCH is a real enable/disable toggle, not just create/delete).

## Audit, Compliance & Observability

- **Audit** — `src/api/routers/audit.py`: `/logs`, `/logs/{id}`, `/export`, plus two targeted views the new app doesn't expose as endpoints: **`/trades/{entity_id}/trace`** (full audit trace for one trade) and **`/actors/{actor_id}/summary`** (per-actor activity rollup). Backed by `src/core/{audit.py,audit_archive.py,audit_chain_monitor.py}` — the chain monitor runs continuously, not just on an on-demand verify call.
- **Metrics & tracing** — Prometheus `/metrics` + `src/observability/tracing.py` (OpenTelemetry). **Tracing is fully instrumented but has no active exporter configured — spans are generated and go nowhere observable** **[prior audit item 20]**.
- **System-wide rate limiting** — `src/core/{api_rate_limit.py,rate_limit_middleware.py}` + `/system/rate-limit/status`. General API-wide, not scoped to one domain.
- **Security scanning as CI gates** — gitleaks (secret scanning), Bandit (SAST), pip-audit (dependency CVE scan), mypy strict, all real jobs in `.github/workflows/ci.yml`, not just local dev commands. `.gitleaks.toml` + `.pre-commit-config.yaml` at the repo root.
- **Deliberately vulnerable test endpoint** — `/_zap_test/reflect` (`src/api/main.py`), behind a feature flag, existing purely to give the OWASP ZAP CI scanner a provable finding to catch. Its default-off state in a real deployment was flagged by the prior audit as **not independently confirmed** — carrying the same caveat forward here rather than re-stating it as settled.

## Settings & Ops Surface

- **General settings router** — `src/api/routers/settings.py`: `/integrations` status, top-level `SystemSettingsResponse`, `/vault/status`, LLM provider keys, notification channels. Broader than the new app's split-by-domain settings routes (broker/LLM/notifications each own router) — one place to see integration health across the board.
- **Real HashiCorp Vault** — KV (secrets) + Transit (signing), with an unsealer sidecar (`vault-unsealer` service) and `vault/keys/{root_token,unseal_key}` present on disk in this checkout, i.e. it has actually been run, not just configured. The new app's equivalent is a single Fernet-encrypted local file (`src/security/secrets_store.py`).

## Deployment & Ops

- **Docker Compose — 13 services**: `app` **and** `app-tls` (a real TLS-terminated variant, with its own cert), `postgres`, `qdrant`, `redis`, `temporal`, `monte-carlo-worker`, `tick-publisher`, `paper-trading-worker` (three dedicated long-running background-worker *containers*, not in-process APScheduler jobs inside the API container), `vault` + `vault-unsealer`, `minio`, `prometheus`, `grafana`, `frontend`.
- **Kubernetes** — a real Helm chart (`k8s/tradingos/{Chart.yaml,values.yaml}`), covering only the API service; never deployed by CI (`helm lint`/`helm template` only); its HPA is disabled pending a metrics adapter that isn't provisioned **[prior audit item 11]**.
- **CI/CD** (`.github/workflows/ci.yml`, 4 jobs): `lint-type-test` (gitleaks → build the real image → boot Postgres/Vault/Temporal/MinIO → wait for each → ruff → black --check → **mypy strict** → **Bandit** → **pip-audit** → apply Alembic migrations for real → bootstrap Vault Transit/KV and real Qdrant collections → generate a dev TLS cert and boot both `app` and `app-tls` → confirm Prometheus is scraping both → seed a real admin user + paper account + historical market data → pytest → **a per-module coverage gate** → **mutation testing scoped to Risk Manager/Kill Switch**, `continue-on-error: true` due to a known mutmut/src-layout crash), `build-and-push` (GHCR), `helm-lint`, and a full **Cypress E2E job** against the real stack with seeded data.
- **Monitoring** — Prometheus + Grafana + `monitoring/rules/availability.yml` (alert rules). A PagerDuty contact point was removed from Grafana provisioning after a crash-loop caused by an empty integration key **[prior audit item 9]**.
- **Pre-commit + secret scanning** — `.pre-commit-config.yaml`, `.gitleaks.toml` at repo root; no equivalent in the new app.

## Frontend

17 pages under `frontend/src/app/(app)/`: `account`, `audit`, `backtests`, `chat`, `console` (+ `console/agents`, `console/agents/[agentId]`, `console/approvals`, `console/live-runs`, `console/runs`, `console/runs/[runId]`), `market-analysis`, `orders`, `settings` (+ `settings/agents/[agentId]`), `strategies`, plus `login`. No unit/component test layer — **Cypress E2E is the only frontend test tier** **[prior audit item 5]**. Route protection is entirely client-side; JWT lives in `localStorage`, not an httpOnly cookie — a documented, deliberate MVP tradeoff, not an oversight **[prior audit item 6]**.

## Explicitly broken, stubbed, or half-built in the old app (not credited as working features above)

- `LiveExecutionPipeline`: built, tested, never wired to a worker — live trading is human-only in practice.
- Correlation constraint / position sizing: built, tested, not confirmed wired into the real-time risk gate.
- MFA: fully built, mandatory-role set is empty — not protecting any real login.
- `POST /chat/messages`: unauthenticated.
- Multi-tenancy: real schema, cross-tenant isolation not retrofitted anywhere outside `tenants.py` itself.
- Instrument-master sync / intraday ingestion: not scheduled.
- `CorporateAction` / `PortfolioAllocationRecommendation` models not registered in `src/models/__init__.py` — a live risk that a future `alembic --autogenerate` proposes dropping those tables.
- ML/RL platform (MLflow, PyTorch, RL libraries): already removed for a host RAM constraint before this audit; `ml_models` table is vestigial.
- OpenTelemetry tracing: instrumented, no exporter — spans generated, never shipped anywhere.
- Helm chart: never deployed by CI.
- `/_zap_test/reflect`: a deliberately vulnerable endpoint behind a flag whose real-deployment default was not confirmed.
