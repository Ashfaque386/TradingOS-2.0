# TradingOS 2.0 — Build Specification & Technical Implementation Document

**Document type:** Greenfield technical build specification. This document is self-contained: it does not assume or reference any existing codebase, and describes what to build, not what already exists.
**Predecessors:** Informed by a feature/requirements analysis of a prior TradingOS build and by research into OpenClaw's agent-operations model (see the companion `TradingOS_2.0_Blueprint.md` for that research and rationale). This document turns those decisions into a concrete architecture, schema, module structure, and build plan.
**Confirmed decisions this spec is built around:**
- Backend: **Python (FastAPI)**.
- Live trading: the agent organization runs **fully autonomously through paper trading** (research → strategy generation → backtesting → paper execution, on live market data, no human in the loop). **Any order that would move real money requires explicit human validation** before it reaches the broker — there is no fully autonomous live-money path in 2.0.
- Deployment: **self-hosted, Docker Compose**, single-operator scale (not Kubernetes).
- **MFA is out of scope for 2.0.** Authentication is password + JWT + RBAC only.

---

## 1. Objectives & Design Principles

1. **Safety-first, human-gated capital.** Every agent, every automation path, and every UI convenience is designed around one rule: nothing may move real money without an explicit, logged human action. Automation is trusted all the way through paper trading; it is never trusted with live capital.
2. **Config-driven agent operations.** The agent organization (identities, routing, skill grants, LLM provider assignment) is controlled through a single versioned configuration file and a CLI, not by editing code.
3. **A visually alive, premium control surface.** The operator's primary experience is a real-time "Mission Control" console with a living visual representation of what the organization is doing right now, built on WebSocket streams rather than polling.
4. **Single-operator scale, production-grade discipline.** No Kubernetes, no multi-tenant complexity — but full audit trails, deterministic risk gates, and a testable, typed codebase throughout.
5. **India-market correctness by construction.** NSE/BSE trading calendar, F&O contract mechanics, SEBI-style position/circuit-band limits, and Indian trading-cost friction (brokerage/STT/GST/stamp duty) are modeled as first-class domain concepts, not bolted on.

---

## 2. High-Level Architecture

```
                         ┌─────────────────────────────┐
                         │   tradingos.config.json      │  ← single source of truth for
                         │   (hot-reloaded, versioned)   │    agents, routing, skills, risk
                         └──────────────┬───────────────┘    thresholds (pointers only)
                                        │
                     ┌──────────────────▼───────────────────┐
                     │           Agent Gateway               │  NEW control-plane service
                     │  config load/validate/hot-reload/CLI  │
                     └──────────────────┬───────────────────┘
                                        │
   ┌────────────────────────────────────▼───────────────────────────────────┐
   │                          Orchestration Engine                          │
   │   Planner → Task Graph → Task Engine → Dependency Resolver → Events    │
   └───────┬───────────────────────────┬──────────────────────┬────────────┘
           │                           │                       │
   ┌───────▼────────┐        ┌─────────▼─────────┐   ┌─────────▼─────────┐
   │  Agent Runtime  │        │   Safety Layer     │   │   Approval &      │
   │  (24 personas,  │        │  Kill Switch,       │   │   Sign-off Queue  │
   │  LangGraph      │───────▶│  Compliance Check,  │──▶│  (human gate)     │
   │  pipeline)      │        │  Naked-Opt Scanner, │   │                   │
   └───────┬────────┘        │  Go-Live Gate       │   └─────────┬─────────┘
           │                  └─────────────────────┘             │
   ┌───────▼────────────────────────────────────────────┐        │
   │      Strategy Engine: Codegen → Sandbox → Backtest   │        │
   │      → Walk-Forward → Monte Carlo → Paper Trading    │        │
   │      (fully autonomous, tick-driven, live data)      │        │
   └───────┬───────────────────────────────────────────┬──┘        │
           │                                             │           │
   ┌───────▼─────────┐                          ┌────────▼───────────▼────┐
   │  Market Data &   │                          │  Live Order Intent       │
   │  Data Lake       │                          │  (human-gated broker     │
   │  (DuckDB/Parquet,│                          │  submission)             │
   │  Postgres refs)  │                          └────────┬─────────────────┘
   └──────────────────┘                                    │
                                                    ┌────────▼────────┐
                                                    │  Broker Adapters │
                                                    │  (Kite/Upstox)   │
                                                    └────────┬────────┘
                                                             │
                                                    ┌────────▼────────┐
                                                    │  Audit Log       │
                                                    │  (hash-chained,  │
                                                    │  WORM-archived)  │
                                                    └──────────────────┘

   ┌─────────────────────────────────────────────────────────────────┐
   │  Mission Control Console (Next.js) — JARVIS-style HUD            │
   │  subscribes to WebSocket event bus for every box above           │
   └─────────────────────────────────────────────────────────────────┘
```

---

## 3. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| Backend language/framework | Python 3.12, FastAPI, Uvicorn (ASGI) | Best-fit ecosystem for quant (pandas, vectorbt, numpy), LLM orchestration (LangGraph), and broker SDKs |
| Agent orchestration | LangGraph (stateful multi-node graph) | Deterministic, inspectable graph execution for the 24-agent pipeline |
| Frontend | Next.js (App Router), React, TypeScript | Matches operator's own stack; SSR where useful, client-rendered console |
| Realtime viz layer | Three.js/WebGL (Mission Control HUD), native WebSocket client | For the "Organization Pulse" orb and live panels |
| Primary datastore | PostgreSQL 16 | System of record: users, strategies, orders, trades, agent config, audit log |
| Historical data lake | DuckDB + Parquet (partitioned `year/month/symbol.parquet`) | Fast in-process OHLCV analytics without a server process |
| Agent memory / RAG | Qdrant (vector DB) | Semantic memory for strategy research, news/sentiment, code templates |
| Pub/sub & live bus | Redis 7 | Tick relay, agent logs, order/organization events (non-durable, real-time) |
| Distributed compute | Temporal (single-node, self-hosted) | Batches Monte Carlo simulation (10,000 paths) across concurrent activities |
| Secrets management | `.env` + OS-level file permissions, encrypted at rest via a lightweight secrets file (e.g., `python-dotenv` + `cryptography` Fernet) | Vault is intentionally dropped for 2.0's self-hosted, single-operator scale — full HashiCorp Vault is operational overhead this deployment doesn't need; a simple encrypted secrets store is sufficient given MFA is also out of scope |
| Audit archive | MinIO (S3-compatible, Object Lock) or a local WORM-emulating append-only file store if MinIO is deemed unnecessary at this scale | Immutable audit retention |
| Observability | Prometheus + Grafana; structured logging via `structlog` | Metrics + dashboards without a heavier APM stack |
| LLM providers | Multi-provider router: Anthropic, OpenAI, Google Gemini, DeepSeek, local via Ollama | Failover chain, cost/latency optimization per task type |
| Scheduling | APScheduler (in-process, IST-aware cron) | 12+ scheduled jobs plus the new Heartbeat |
| Containerization | Docker + Docker Compose | Single-operator self-hosted deployment target |
| Strategy sandbox | gVisor or Firecracker microVM (upgrade from subprocess-only isolation) | True isolation for LLM-generated, untrusted Python strategy code — see §9 |
| CI | GitHub Actions (lint, type-check, test, build, image push) | No CD to a live cluster required at this scale; deploy is a manual `docker compose pull && up` |

---

## 4. Repository Structure

```
tradingos/
├── backend/
│   ├── src/
│   │   ├── core/            # config, db, security, audit, secrets, rate-limit
│   │   ├── gateway/         # NEW — Agent Gateway: config schema, loader, hot-reload, CLI
│   │   ├── orchestration/   # planner, task_engine, run_manager, approvals, events, handoffs
│   │   ├── agents/          # LangGraph nodes, persona/identity, llm_router, skills
│   │   ├── engine/
│   │   │   ├── backtest/
│   │   │   ├── optimization/
│   │   │   ├── risk/
│   │   │   ├── paper_trading/
│   │   │   ├── live/            # order-intent generation + human-gate integration
│   │   │   └── sandbox/
│   │   ├── brokers/          # broker adapter ABC + Kite/Upstox implementations
│   │   ├── data/              # ingestion pipelines, data lake access
│   │   ├── memory/            # Qdrant collections, embeddings
│   │   ├── workers/           # Temporal workers, tick publisher, paper-trading worker
│   │   ├── models/            # SQLAlchemy models
│   │   ├── api/                # FastAPI routers
│   │   └── observability/     # metrics, tracing, logging setup
│   ├── alembic/                # migrations
│   ├── tests/
│   └── scripts/
├── frontend/
│   ├── src/
│   │   ├── app/                 # Next.js App Router pages (console, strategies, backtests, orders, settings)
│   │   ├── components/
│   │   │   ├── console/           # Mission Kanban, activity feed, agent tree, sign-off queue
│   │   │   ├── hud/                # Organization Pulse orb, theming, audio layer, system monitor
│   │   │   └── ui/                  # shared design-system primitives
│   │   ├── hooks/                  # WebSocket stream hooks
│   │   └── lib/                     # auth, permissions, stores
├── config/
│   └── tradingos.config.json       # the Agent Gateway config file (schema in §6)
├── docker-compose.yml
├── docker-compose.tls.yml
└── docs/
```

---

## 5. Data Architecture

### 5.1 PostgreSQL — core entity groups

| Domain | Tables | Key notes |
|---|---|---|
| Identity & auth | `users`, `refresh_tokens` | No MFA tables — password + refresh-token rotation only |
| Accounts / broker | `accounts`, `account_equity_snapshots`, `broker_credentials` | Encrypted broker credential storage |
| Agent Gateway (NEW) | `agent_identities` (name/emoji/avatar/theme/voice per agent), `agent_bindings` (channel↔persona routing), `agent_config_versions` (config-file version history), `agent_to_agent_policy` | Backs the config layer in §6 |
| Agent execution | `agent_runs`, `agent_logs`, `agent_control_state`, `agent_configs`, `prompt_versions` | |
| CEO-organization layer | `organization_runs`, `organizational_plans`, `tasks`, `task_dependencies`, `result_artefacts`, `organizational_decisions`, `organizational_events`, `approval_requests` | `approval_requests` gains a `queue_type` enum: `strategy_promotion` \| `live_order_intent` \| `config_change` |
| Strategy pipeline | `strategies`, `strategy_versions`, `backtest_results`, `strategy_suggestions` | `strategies.status` uses a DB-level CHECK constraint (fixes a known 1.0-class gap): `Ideation/Coding/Backtesting/PaperTrading/LiveEligible/Live/Deprecated` |
| Paper trading | `paper_trades`, `shadow_mode_attempts` | |
| Core trading | `orders`, `trades`, `portfolio_positions`, `risk_limits`, **`live_order_intents`** (NEW) | `live_order_intents` is the human-gate queue table — see §12 |
| Governance | `audit_log` (hash-chained, append-only), `risk_limit_change_requests` | |
| Reference data | `instruments`, `market_data_provenance`, `corporate_actions`, `dataset_freshness_records` | |
| Chat / webhook | `chat_messages`, `webhook_events` | |
| Scheduling | `scheduled_job_config`, `scheduled_job_run` | Includes the new `heartbeat` job type |
| Skills (marketplace) | `skills`, `agent_skill_map`, `skill_versions` (NEW) | |
| Portfolio advisory | `portfolio_allocation_recommendations` | |

Every table's model module must be imported by the package `__init__.py` so Alembic autogenerate always sees the full metadata (a defect class explicitly designed out from day one).

### 5.2 DuckDB / Parquet data lake
Partitioned `year/month/symbol.parquet`; two nightly-refreshed catalog views (`ohlcv_daily`, `ohlcv_intraday`); nightly checksum + row-count-validated backup.

### 5.3 Qdrant collections
`trading_strategies`, `agent_memory`, `news_sentiment`, `code_templates`, `organization_memory` — embeddings via local Ollama (`nomic-embed-text`, dim 768) with a CPU-only sentence-transformers fallback (dim 384) for network-free operation.

### 5.4 Redis usage
Tick relay (`ticks:<symbol>` pub/sub), agent log stream, order-event stream, organization-event stream, and the new org-wide activity feed stream consumed by the Mission Control Console.

---

## 6. Agent Gateway & Configuration Layer

### 6.1 Config file schema (`config/tradingos.config.json`, JSON5-tolerant)

```json5
{
  "version": 1,
  "infra": {
    "llmProviders": {
      "order": ["anthropic", "openai", "gemini", "deepseek", "ollama"],
      "fallbackPolicy": "next-on-failure"
    },
    "brokerFailover": { "primary": "zerodha", "fallback": "upstox" },
    "riskThresholdRefs": { "maxDrawdownPct": 15, "wsLatencyMs": 100 }
    // NOTE: actual risk-limit values are never mutated via hot reload —
    // this section only points at the dual-control-gated values in Postgres
  },
  "agents": {
    "defaults": {
      "model": "auto",
      "heartbeatEnabled": false,
      "skills": ["market-data-read", "notification-send"]
    },
    "entries": {
      "ceo-agent": {
        "identity": { "name": "CEO", "emoji": "🧠", "theme": "cyan", "voice": null },
        "heartbeatEnabled": true,
        "heartbeatIntervalMinutes": 10,
        "model": "custom:anthropic/claude-opus"
      },
      "risk-manager": {
        "identity": { "name": "Risk Manager", "emoji": "🛡️", "theme": "amber" },
        "heartbeatEnabled": true,
        "heartbeatIntervalMinutes": 5
      }
      // ... one entry per agent, all optional-override
    }
  },
  "bindings": [
    { "agentId": "ceo-agent", "match": { "channel": "telegram", "accountId": "ops" } }
  ],
  "agentToAgentPolicy": {
    "default": "deny",
    "allow": [
      { "from": "risk-manager", "to": "ceo-agent", "scope": "read-artefacts" }
    ]
  }
}
```

### 6.2 Loader & hot reload
- File watcher (`watchdog`) detects changes; the Gateway validates against a Pydantic-backed JSON Schema before applying anything.
- On validation failure: reject the change, keep running on the last-known-good in-memory config, write an `agent_config_versions` row marked `rejected` with the validation errors, and surface it in the Mission Control Console.
- On success: apply non-capital-affecting fields immediately (identity, bindings, heartbeat toggles, skill grants, LLM provider assignment); write a new `agent_config_versions` row marked `active`; emit an audit-log entry.
- Fields under `infra.riskThresholdRefs` are **read-only pointers** — changing the actual limit value still requires the existing dual-control stage→confirm API flow (§8.5); the config file cannot set a risk threshold directly.

### 6.3 CLI (`tradingos-cli`)

| Command | Purpose |
|---|---|
| `tradingos-cli agents list [--json]` | List all 24 agents with status, model, heartbeat state |
| `tradingos-cli agents set-identity --agent <id> --name --emoji --avatar --theme --voice` | Update persona fields |
| `tradingos-cli agents bind --agent <id> --channel <ch> [--account <id>]` | Add a channel→persona binding |
| `tradingos-cli agents unbind ...` | Remove a binding |
| `tradingos-cli agents skills grant/revoke --agent <id> --skill <name>` | Manage per-agent skill grants |
| `tradingos-cli agents heartbeat enable/disable --agent <id> --interval <min>` | Toggle/tune heartbeat |
| `tradingos-cli config validate [--file path]` | Dry-run schema validation without applying |
| `tradingos-cli config rollback --to-version <n>` | Roll back to a prior `agent_config_versions` row |
| `tradingos-cli doctor --fix` | Detect and repair known-safe config issues |

The CLI is a thin wrapper over the same FastAPI endpoints the Mission Control Console's Config tab calls — one implementation, two entry points.

### 6.4 Agent-to-agent access policy
Default-deny. Every cross-agent read of another agent's artefacts, logs, or session state must match an explicit `allow` rule (`from`, `to`, `scope`). Enforced at the ORM/query layer in `orchestration/handoffs.py`-equivalent module, not just in the API layer.

---

## 7. Agent Organization & Orchestration

### 7.1 Fixed 24-agent roster (curated, not user-creatable)

| Department | Agents |
|---|---|
| Executive | CEO Agent, CEO Chat Interface |
| Market Intelligence | Market Analyst, News Agent (scheduled, no LLM), Sentiment Agent |
| Research | Strategy Generator, Options Strategy Agent |
| Quant | Python Code Generator, Python Validator (deterministic), Backtesting (deterministic), Optimization (deterministic), Evaluator |
| Risk & Governance | Risk Manager, Compliance, Audit Agent (cannot be disabled), Deployment |
| Portfolio | Portfolio Manager Agent (advisory) |
| Operations | Memory Agent, Data Ingestion Agent, Scheduler Agent, Notification Agent, Skill Registry Manager, Execution Agent, Paper Trading Engine (scheduled) |

The Agent Gateway (§6) configures these 24; it does not create or delete them — the roster is fixed at design time because each agent maps to a specific, reviewed role in the pipeline below.

### 7.2 LangGraph pipeline (strategy research → deployment)

```
CEO → Market Analyst → Strategy Generator → Options Strategy Agent →
Python Code Generator → Compliance →[Block→END]→ Python Validator →[3 fails→END]→
Backtesting → Evaluator →[PASS]→ Optimization → Risk Manager → Deployment → END
                    │
                    └─[FAIL]→ Memory Ingest → [5 rejections→CEO | else→Strategy Generator]
```

- State object: a strict, versioned Pydantic model (`TradingOSGraphState`) — every node reads/writes typed fields only, no ad hoc dict mutation.
- Every node wrapped in a circuit-breaker check: a disabled agent's node logic never executes, enforced at graph-build time, not just by convention.

### 7.3 Orchestration engine components

| Component | Responsibility |
|---|---|
| Planner | Given an objective (UI/schedule/chat/webhook), calls the LLM to produce a task graph; deterministically validates: acyclic, capability-matched, fixed safety order (`code_validation → compliance_check → backtesting → strategy_evaluation → risk_assessment`); up to 3 LLM attempts before `cannot_plan` |
| Research scaffold injector | Always prepends news → sentiment → market-analysis → portfolio-read tasks ahead of any LLM-planned task, resolved by capability |
| Task engine | Postgres-advisory-lock task claiming; thread-pool dispatch for concurrency-safe capabilities; per-task timeouts; stall detection (5-min sweep, 900s threshold) |
| Dependency resolver | Tasks wait on typed artefact dependencies; wake on completion; mark unsatisfiable on permanent failure |
| Run control | Pause / Continue / Retry (transient-only) / Rerun (always a fresh CEO re-plan) with race-safe conditional updates |
| Crash recovery | On every boot, re-enter any run left non-terminal by a prior crash |
| Approvals | Strategy promotion (Backtesting→PaperTrading) requires human sign-off; **no code path may skip this**, enforced server-side at the status-transition function, not just at one API route |
| Conflict detection | Deterministic checks: market-vs-sentiment divergence, risk-reject-vs-proposal, compliance-block-vs-proposal |
| Event bus | Single call does Postgres insert + Redis publish + audit entry, with gap-free per-run sequencing |

---

## 8. Risk & Safety Layer (deterministic, sits above the LLM agents)

| Control | Spec | Threshold (default, configurable via dual-control) |
|---|---|---|
| Max Drawdown Kill Switch | Latching; once tripped, halts all order placement (paper and live) until a human explicitly resets it | 15% |
| Paper Kill Switch | Separate instance from the live/production switch — must never share state | 15% (independently configurable) |
| WebSocket Latency Guard | Self-clearing pause when tick-relay latency exceeds threshold | 100ms |
| Compliance pre-trade check | SEBI-style position limits + NSE-style circuit-filter band + naked-options scan; deterministic, independent of LLM judgment | Data source: start with a maintained reference table; design the interface so a live regulatory feed can be swapped in later without a schema change |
| Naked-options scanner | Every sold option leg must have a same-underlying OTM hedge | — |
| Correlation constraint | Rejects proposed strategies/positions correlated to Nifty 50 beyond a threshold — **wired into both the pre-deployment check AND the live/paper per-tick risk gate** (closing a known gap-class from the prior build) | 0.85 |
| Volatility-adjusted position sizing | Inverse-volatility capital allocation | — |
| Broker circuit breaker | Opens after N consecutive 5xx from a broker; cooldown period; alert fan-out | 3 failures / 1-min cooldown |
| Go-Live Readiness Gate | ALL required: minimum trade count, minimum elapsed calendar days, minimum clean Shadow Mode streak, bounded live/backtest win-rate divergence | ≥30 trades, ≥21 days, ≥10-day streak, ≤20pp divergence (tunable) |
| Shadow Mode | Pre-live dry-run against a real broker sandbox where one exists; honestly differentiated (local-construction-only) where no sandbox exists | — |
| Dual-control risk limits | Stage by one privileged user → confirm by a **different** privileged user → apply; self-confirm rejected | — |

### 8.5 Dual-control flow (unchanged principle, specified fresh)
1. `POST /risk-limits/stage` — any RiskManager/SystemAdministrator proposes a new limit value with a reason.
2. `POST /risk-limits/{id}/confirm` — must be called by a **different** user with the same or higher privilege; self-confirmation returns 403.
3. Only on confirm does the limit apply; every step writes an audit-log row.

---

## 9. Strategy Pipeline & Sandbox

| Stage | Spec |
|---|---|
| Code generation | LLM produces a Python module implementing a fixed contract: `run_backtest(data: pd.DataFrame, config: dict) -> dict` |
| Static validation | AST ban-list (no `os`, `subprocess`, `socket`, `eval`, `exec`, dynamic imports outside an allowlist) + `ruff` lint |
| **Sandbox execution (upgraded)** | Runs inside a **gVisor or Firecracker microVM**, not a bare subprocess — true kernel-level isolation for untrusted, LLM-generated code. CPU/memory rlimits, no network access, restricted filesystem (read-only mount of the data lake, writable scratch dir only), execution timeout | 
| Warm pool | A pool of pre-initialized sandbox workers to avoid multi-second cold-start latency on every backtest call |
| Human suggestions | Free-text improvement request → AI review verdict → optional regeneration, same diff-gated activation pattern as prompt versioning |
| Options legs grounding | Options Strategy Agent grounds legs against a live option chain; naked-options scan runs before acceptance |

---

## 10. Backtesting & Optimization Engine

| Component | Spec |
|---|---|
| Backtest engine | Vectorized single-symbol backtesting (e.g., `vectorbt`-style), never fabricates a metric — non-finite results return `null` |
| Indian friction model | Brokerage, STT, exchange charges, SEBI turnover fees, stamp duty, GST, plus dynamic ATR/volume-based slippage |
| Corporate-action adjustment | Back-adjusts OHLCV for splits/bonuses; dividends are explicitly out of scope for price adjustment (documented limitation, not a silent gap) |
| Data-freshness gate | Refuses to backtest if the data lake lacks the prior trading day's data |
| Walk-Forward Optimization | Rolling train/test windows; a strategy must show positive out-of-sample expectancy in **every** window to pass |
| Monte Carlo simulation | 10,000 trade-resampled paths; 95th-percentile Max Drawdown (not historical MaxDD) governs risk sizing; distributed via Temporal |
| Hyperparameter sweep | Optuna with fANOVA parameter-importance; real per-trial execution, no shortcutting |
| Multi-run comparison | Pairwise return-correlation matrix; `null` (never fabricated 0/1) when fewer than 10 overlapping days |

---

## 11. Paper Trading Engine (fully autonomous)

| Layer | Spec |
|---|---|
| Layer 1 — Daily signal | Re-runs the full backtest once/day on the latest data; reads the entries/exits transition to generate BUY/SELL signals |
| Layer 2 — Intraday | Tick-driven universal stop-loss + re-entry logic, fed by live Redis ticks sourced from real broker quotes |
| Fill simulation | Depth-walked against real Level-2 quotes; partial fills surfaced honestly when depth is insufficient |
| Position accounting | Average-cost-basis ledger (explicit simplification vs. FIFO lot matching, documented) |
| Margin model | Approximate F&O margin (flat futures %, zero for long options, full notional for short) — explicitly not a real SPAN calculation |
| F&O multi-leg | No atomic multi-leg primitive; a partial fill across legs is surfaced, not silently retried |
| Autonomy | This entire layer runs with **zero human approval required per trade** — it is the automated proving ground the product owner explicitly wants fully hands-off |

---

## 12. Live Trading & Order Management — Human-Gated by Design

This is the section that operationalizes the product owner's decision: full autonomy through paper trading, mandatory human validation for anything that touches real money.

### 12.1 Flow

```
[Strategy passes Go-Live Readiness Gate]
        │
        ▼
[Human sign-off: "approve strategy for live eligibility"]   ← one-time per strategy
        │
        ▼
[LiveExecutionPipeline generates order INTENTS automatically]
  (same tick-driven signal logic as the paper engine, running live)
        │
        ▼
[live_order_intents table — status: PENDING_APPROVAL]
        │  appears in the Mission Control Sign-off Queue in real time,
        │  with a configurable expiry window (default 90 seconds)
        ▼
   ┌────────────────┬─────────────────────┐
   │  Human APPROVES │  Human REJECTS or   │
   │  within window  │  window expires     │
   ▼                 ▼
[Broker submission]  [Intent marked EXPIRED/REJECTED — no order placed]
        │
        ▼
[Order/Trade rows written; hash-chained audit entry]
```

### 12.2 Rules
- A strategy being "live-eligible" (passed the Go-Live Gate + human sign-off) is necessary but never sufficient — every individual order intent still requires per-intent human approval.
- The expiry window is configurable per strategy (some setups tolerate a 2-minute window; fast-moving intraday setups may need 30 seconds) but has a hard platform-wide **maximum** of 5 minutes — an intent that a human hasn't acted on is designed to expire into "no trade," never into "auto-trade."
- A human may also pre-authorize a **bounded batch approval** (e.g., "approve up to 3 intents for Strategy X in the next 30 minutes, max ₹Y notional each") to reduce click-fatigue during active sessions — this is still an explicit, logged human action taken in advance, not silent automation; the bounds are enforced server-side, not just suggested in the UI.
- Manual order placement (a human directly placing/canceling a live order outside the agent pipeline) remains available via the API exactly as a manual trading terminal would, independent of the intent-queue flow.
- The Kill Switch, once tripped, halts new order **intents** from being generated at all — it doesn't just block submission at the approval step.

### 12.3 New table: `live_order_intents`
Columns: `id`, `strategy_id`, `symbol`, `side`, `quantity`, `intent_type` (`entry`/`exit`/`stop`), `generated_at`, `expires_at`, `status` (`pending_approval`/`approved`/`rejected`/`expired`/`submitted`/`failed`), `approved_by`, `approved_at`, `resulting_order_id`, `batch_authorization_id` (nullable, for the pre-authorized batch flow).

---

## 13. Broker Integration Layer

| Broker | Operations | Notes |
|---|---|---|
| Zerodha Kite Connect | Place/modify/cancel order, order book, margin, positions, quote, option chain, expiries | Daily OAuth; no sandbox — Shadow Mode here is a local payload construction only, documented as such |
| Upstox (Trading V2) | Same operation set + instrument search + historical candles | Has a real sandbox — Shadow Mode makes genuine dry-run calls |
| Upstox (Market Data V3) | Historical/latest data, health check | Read-only, always production |
| Broker abstraction | A single `BrokerAdapter` interface; `BrokerCircuitBreaker` with primary/fallback; credential lookup from the secrets store |

---

## 14. Market Data & Data Lake Pipelines

| Pipeline | Trigger | Notes |
|---|---|---|
| Scheduled incremental ingestion | Daily cron, IST, skips NSE holidays | |
| Corporate actions ingestion | Daily cron | |
| Instrument master sync | **Scheduled**, not manual-only (fixes a known gap-class) | |
| Intraday minute-bar ingestion | **Scheduled**, not dormant (fixes a known gap-class) | |
| NSE Bhavcopy EOD / F&O bhavcopy | Manual/on-demand fallback | Free official daily data |
| DuckDB catalog refresh | Nightly | |
| Data lake backup | Nightly, checksum + row-count validated | |
| Market Pulse | On-demand | India VIX, sector indices, global index day-change |

---

## 15. Mission Control Console & JARVIS-Style HUD

### 15.1 Information architecture

| Section | Contents |
|---|---|
| Overview | Organization Pulse orb, KPI strip, health status |
| Live Runs | Active organization runs, task DAG, activity stream |
| Mission Kanban | Inbox → Planning → In-Progress → Backtesting/Review → Sign-off → Live/Done, over the run/task/approval state machine |
| Sign-off Queue | Strategy promotions AND live order intents (§12), each with full context and a countdown for time-bound intents |
| Agent Fleet | Hierarchy tree (CEO → 6 departments → 24 agents), live/idle/disabled status, click-through to per-agent workspace |
| Per-agent workspace | Identity editor, model/provider override, prompt version history + diff, skill grants, heartbeat config |
| Strategies | Kanban lifecycle view, code diff, review panel |
| Backtests | Metric grid, equity curve vs. Nifty 50, drawdown, cross-run comparison, correlation matrix, Monte Carlo histogram |
| Orders & Trades | Unified paper+live history, live order intent queue, latency stats |
| Market Analysis | Pulse, technical indicators, data freshness, option chain |
| Audit Log | Filterable, exportable (CSV/NDJSON) |
| Settings | Broker/LLM credentials, notification channels, Agent Gateway config editor (form + raw JSON) |

### 15.2 JARVIS HUD visual layer

| Element | Spec |
|---|---|
| Organization Pulse orb | WebGL/Three.js centerpiece; states: Idle, Researching, Executing (Paper), Awaiting-Approval (Live Intent Pending), Risk-Alert; color + particle density scale with severity |
| Color theme system | 6 selectable palettes (cyan/green "nominal", amber "caution", red "risk/kill-switch", plus neutral dark, light, and a high-contrast accessibility mode), one-click switch, applied via CSS custom properties/HSL rotation |
| Audio-reactive alerts | Optional, off by default; spectrum/ring/waveform visualization paired with TTS voice alerts; every audio/visual alert has a mandatory text-channel equivalent (toast + Telegram/Discord/Slack) |
| Live system monitor panel | CPU/mem/uptime, active LLM provider + token usage, order-dispatch latency — sourced from the Prometheus metrics endpoint via SSE |
| Memory timeline | Day-grouped view over the `organization_memory`/`agent_memory` Qdrant collections |
| Glass/neon premium design system | Translucent panels, glow borders, motion-designed transitions, dark-first with a light mode |
| Power-save mode | Throttles to 15fps, disables particles/glow; persisted per operator; recommended default ON during market hours |
| Responsive/mobile | Touch gestures, collapsible orb, swipeable panels |

### 15.3 Real-time data contract
All HUD/Console panels subscribe to typed WebSocket channels: `ticks`, `agent-logs`, `order-events`, `organization-events`, `activity-feed` (new, org-wide), `sign-off-queue` (new, pushes both strategy-promotion and live-intent items with expiry countdowns).

---

## 16. Skill Marketplace (internal, curated)

| Feature | Spec |
|---|---|
| Skill catalog UI | Searchable list/card view; health indicator (healthy/degraded/disabled) |
| Skill definition | Versioned module with metadata (name, description, required credentials, sandbox requirements) |
| Grant matrix | Agents × skills grid for at-a-glance auditing |
| Hot enable/disable | No restart required |
| Provenance | **No external ingestion path** — every skill is authored and reviewed in-house before it can be marked installable |

Starter skill set (carried forward as domain requirements, re-specified fresh): market-data-read, option-chain-read, portfolio-status-read, code-format/lint, sandbox-dry-run, notification-send (Telegram/Discord/Slack).

---

## 17. Automation: Scheduler & Heartbeat

| Feature | Spec |
|---|---|
| Cron scheduler | APScheduler, IST-aware, DB-overridable per job, run-now + execution history in the UI |
| Heartbeat (NEW) | Configurable-interval, read-only self-check per eligible agent (CEO, Risk Manager by default); may raise alerts/open tasks; **structurally cannot call any order-placement or risk-limit-mutation code path** — enforced by giving the heartbeat execution context no reference to those functions, not just a permission check |
| Outbound webhooks | Signed payload on job completion/failure, for external ops tooling |

---

## 18. Notifications & Omni-Channel

| Feature | Spec |
|---|---|
| Channels | Telegram, Discord, Slack (webhooks + bot tokens) — deliberately not expanded beyond these plus in-app chat (§9 of the Blueprint document explains the scoping rationale) |
| Inbound security | Signature verification per channel (secret-token / Ed25519 / HMAC-SHA256), replay-guarded, rate-limited |
| Routing | Verified sender → CEO Agent → optional organization run via message classification |
| Outbound | Same channels for alerts (kill-switch trips, sign-off items, go-live gate passes) |
| In-app chat | Streaming responses, abort, per-session model switch, search, pin, export |

---

## 19. Audit, Compliance & Observability

| Feature | Spec |
|---|---|
| Audit log | Hash-chained (SHA-256), DB-trigger-enforced append-only, advisory-lock-serialized writers |
| WORM archive | Object-lock-style immutable archival (MinIO or equivalent), periodic chain-divergence verification |
| Export | CSV/NDJSON, filtered by entity/actor, role-restricted |
| Metrics | Prometheus histograms: WS latency, agent node duration, order dispatch latency, LLM token usage, trading-holiday gauge |
| Dashboards | Grafana: WS latency, agent duration, order latency, market-hours uptime |
| Alerting | Market-hours downtime rule at minimum, fanned to Telegram/Discord/Slack |
| Logging | Structured (`structlog`), correlation IDs threaded through orchestration → agent → order flow |

---

## 20. Security (MFA explicitly out of scope)

| Control | Spec |
|---|---|
| Authentication | Email/password → short-lived JWT access token (15 min) + rotating refresh token (7-day TTL) |
| Refresh token security | Single-use, reuse-detection revokes the entire token family |
| RBAC | 4 roles (SystemAdministrator, PortfolioManager, RiskManager, ReadOnlyAuditor), policy-engine-enforced (e.g., Casbin), exact route+method matching |
| Dual control | Required for risk-limit changes (§8.5); a different privileged user must confirm |
| Secrets | Encrypted-at-rest local secrets store (broker/LLM credentials); never logged, never returned in API responses except as write-only fields |
| Webhook security | Cryptographic signature verification + replay protection on every inbound channel |
| Transport | TLS termination (self-signed acceptable for local/self-hosted; real cert recommended if exposed beyond localhost) |
| Gateway/Console exposure | Binds to localhost or an internal network interface by default; if ever exposed beyond that, requires a reverse proxy with its own access control — the Agent Gateway's config/CLI surface is not designed to be internet-facing |
| Sandbox isolation | gVisor/Firecracker microVM for all untrusted strategy code execution (§9) |
| Skill provenance | Internal-only, reviewed skills (§16) — no open marketplace ingestion |

---

## 21. Non-Functional Requirements

- **Performance:** order-dispatch latency (tick → broker handoff, where applicable) measured against a documented budget and exposed as a Prometheus histogram; sandbox warm-pool keeps repeated backtest/optimization calls fast.
- **Reliability:** broker circuit breaker with fallback; provider-level LLM failover; crash-safe organization-run recovery on every boot; idempotent seed/migration scripts.
- **Auditability:** no committed audit-log row is mutable or deletable through any application code path, including the new config-change and live-order-intent flows.
- **Data integrity:** backtests refuse to run on stale data; migrations are additive/backward-compatible, destructive changes only in `downgrade()`.
- **Testability:** risk-critical modules (Kill Switch, live-intent-expiry logic) get mutation testing in addition to unit/integration coverage.
- **Accessibility:** HUD color themes include a high-contrast/reduced-motion mode meeting WCAG 2.1 AA — risk states must stay distinguishable without relying on color alone.
- **Deployability:** the entire stack starts from `docker compose up`; no manual multi-step bootstrap beyond seeding initial admin credentials and broker API keys.

---

## 22. Testing Strategy

| Layer | Approach |
|---|---|
| Unit | Every risk module (Kill Switch, Compliance Checker, correlation constraint, live-intent expiry) at ≥90% coverage; mutation-tested |
| Integration | Full LangGraph pipeline run against fixture market data, asserting the safety-order sequencing is unbypassable |
| API/contract | Every mutating endpoint tested for RBAC enforcement, including the new config/CLI-backed endpoints |
| Sandbox security | Adversarial test suite attempting filesystem/network escape from generated strategy code, run against the gVisor/Firecracker sandbox |
| End-to-end | Cypress (or Playwright) covering: strategy lifecycle, sign-off queue (both strategy promotion and live-intent expiry/approval paths), Kill Switch trip-and-reset, Agent Gateway config hot-reload with an intentionally invalid config |
| Load | Backtest/optimization throughput under concurrent runs; WebSocket fan-out under many simultaneous Console connections (low priority at single-operator scale, but a smoke test is worthwhile) |

---

## 23. Docker Compose Service Inventory

| Service | Purpose |
|---|---|
| `api` | FastAPI backend, scheduler, hot-reload watcher |
| `postgres` | System of record |
| `qdrant` | Vector memory |
| `redis` | Pub/sub |
| `temporal` | Monte Carlo distributed compute |
| `monte-carlo-worker` | Temporal worker |
| `tick-publisher` | Broker-quote polling → Redis ticks |
| `paper-trading-worker` | Layer-2 intraday paper sync loop |
| `live-intent-worker` | NEW — generates live order intents and manages the sign-off/expiry lifecycle |
| `sandbox-runtime` | gVisor/Firecracker execution host for strategy code |
| `minio` | Audit archive (optional at smallest scale; can start with local append-only file storage and add MinIO later) |
| `prometheus` / `grafana` | Metrics + dashboards |
| `frontend` | Next.js Mission Control Console |

---

## 24. Build Phases

| Phase | Deliverable |
|---|---|
| 0 | Repo scaffold, Docker Compose skeleton, Postgres schema + Alembic baseline, auth (password/JWT/RBAC, no MFA) |
| 1 | Agent Gateway: config schema, loader, hot reload, CLI, `agent_identities`/`agent_bindings`/`agent_config_versions` tables |
| 2 | Orchestration engine: planner, task engine, dependency resolver, run control, approvals, events |
| 3 | 24-agent LangGraph pipeline + LLM router + skill registry (internal marketplace UX) |
| 4 | Strategy pipeline: codegen, validation, gVisor/Firecracker sandbox, backtest engine, Indian friction model |
| 5 | Optimization: Walk-Forward, Monte Carlo (Temporal), Optuna sweep |
| 6 | Risk & safety layer: Kill Switch, Compliance Checker, naked-options scanner, correlation constraint (fully wired), Go-Live Gate, dual-control risk limits |
| 7 | Paper trading engine (Layers 1 & 2), fully autonomous |
| 8 | Broker integrations (Kite, Upstox), circuit breaker, Shadow Mode |
| 9 | **Live order intent pipeline + Sign-off Queue** (§12) — the human-gate flow |
| 10 | Market data & data lake pipelines, scheduling |
| 11 | Audit log (hash-chained + archive), observability (Prometheus/Grafana/structlog) |
| 12 | Notifications & omni-channel (Telegram/Discord/Slack), in-app chat |
| 13 | Mission Control Console: IA, Kanban, sign-off queue UI, agent fleet, activity feed |
| 14 | JARVIS HUD: Organization Pulse orb, theming, audio layer, system monitor, memory timeline, power-save mode |
| 15 | Security/testing hardening pass: sandbox adversarial tests, mutation testing on risk modules, E2E suite, accessibility pass |

---

## 25. Appendix — Glossary (fresh terms introduced in this spec)

| Term | Meaning |
|---|---|
| Agent Gateway | The config-driven control-plane service owning agent identity, routing, and skill grants |
| Live Order Intent | A system-generated, not-yet-submitted live trade proposal awaiting human approval or expiry |
| Sign-off Queue | The unified human-gate UI surface for both strategy promotions and live order intents |
| Organization Pulse | The central live visualization reflecting overall system state |
| Heartbeat | A proactive, strictly read-only periodic self-check, structurally incapable of moving money |
| Batch Authorization | A bounded, pre-logged human pre-approval covering multiple future order intents within stated limits |

*End of document. This is a build specification for implementation planning — each numbered section should become one or more engineering epics; §24's phase order is a recommended sequence, not a rigid dependency chain (e.g., HUD work in Phase 14 can start once the WebSocket contracts from Phase 2/13 exist, in parallel with later backend phases).*
