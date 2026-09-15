# TradingOS 2.0 — Claude Code Build Prompt Pack

**How to use this:** Put `CLAUDE.md` at the repo root and this file (plus the Blueprint and Build Specification) under `docs/` before starting Phase 0 — Claude Code reads `CLAUDE.md` automatically every session. Then work through the phases below **one at a time, in order**, pasting each phase's prompt as a fresh instruction once the previous phase is reviewed and working. Don't paste multiple phases at once — each one should end with working, tested code before you move on. After each phase, update the checklist in `CLAUDE.md`.

Each prompt assumes Claude Code has repo access and can run shell commands, tests, and migrations directly.

---

## Phase 0 — Repo & Foundations

```
Set up the TradingOS 2.0 backend from scratch per docs/TradingOS_2.0_Build_Specification.md §3 (tech stack) and §4 (repo structure).

1. Scaffold the backend/ directory tree exactly as laid out in §4: core, gateway, orchestration, agents, engine/{backtest,optimization,risk,paper_trading,live,sandbox}, brokers, data, memory, workers, models, api, observability, plus alembic/, tests/, scripts/.
2. Set up Python 3.12 + FastAPI + Uvicorn, with pyproject.toml (or requirements.txt) pinning SQLAlchemy, Alembic, Pydantic v2, pytest, ruff, structlog, python-jose (or equivalent) for JWT.
3. Postgres 16 connection via src/core/db.py (async SQLAlchemy engine + session dependency).
4. Implement auth per Build Spec §20: users table (email, hashed password, role), refresh_tokens table with reuse-detection (single-use, revoking the whole family on reuse), JWT access (15 min) + refresh (7 day) issuance/verification in src/core/security.py. NO MFA — do not add TOTP scaffolding.
5. Implement RBAC for the 4 roles (SystemAdministrator, PortfolioManager, RiskManager, ReadOnlyAuditor) — a policy-engine approach (Casbin or an equivalent lightweight route+method+role table) enforced via a FastAPI dependency, not per-route ad hoc checks.
6. Set up Alembic with a baseline migration creating users and refresh_tokens. Make sure every future model module gets imported by src/models/__init__.py — flag this requirement in a comment at the top of that file so it isn't forgotten.
7. Docker Compose: api, postgres services only for now (more services join in later phases) — docker compose up should give a working, migrated, empty-but-running API with a health check endpoint.
8. Write tests: registration/login/refresh/reuse-detection, RBAC dependency behavior (a RiskManager hitting a SystemAdministrator-only route gets 403).

Acceptance: docker compose up works, alembic upgrade head works, pytest passes, and I can register a user, log in, refresh, and hit a role-gated endpoint correctly allowed/denied.
```

---

## Phase 1 — Agent Gateway

```
Implement the Agent Gateway per Build Spec §6.

1. Define the config schema (§6.1) as Pydantic models — infra, agents.defaults, agents.entries, bindings, agentToAgentPolicy — with full validation (unknown keys rejected, types enforced).
2. Config loader in src/gateway/: reads config/tradingos.config.json (JSON5-tolerant — use a JSON5 parser, don't hand-roll comment-stripping), validates on load, and exposes the parsed, merged (defaults + per-agent overrides) config to the rest of the app.
3. Hot reload: a file watcher (watchdog) that re-validates on change. On failure: keep the last-known-good config in memory, write an agent_config_versions row with status=rejected and the validation errors, do not crash the running app. On success: apply, write agent_config_versions status=active, emit an audit-log entry.
4. New tables (add to src/models/, migrate): agent_identities, agent_bindings, agent_config_versions, agent_to_agent_policy — per Build Spec §5.1.
5. Enforce that infra.riskThresholdRefs in the config file is read-only from the Gateway's perspective — it may only be changed through the (not-yet-built, comes in Phase 6) dual-control risk-limit API. For now, just make sure the Gateway's config-apply code path has no function that could write a risk threshold value — don't stub it in a way that's easy to accidentally wire up later.
6. CLI (src/gateway/cli.py, exposed as a `tradingos-cli` console script): agents list, set-identity, bind, unbind, skills grant/revoke, heartbeat enable/disable, config validate, config rollback --to-version, doctor --fix. Implement these as thin wrappers over the same functions the future API endpoints will call (§6.3) — write the underlying functions first, then both the CLI and (in a later phase) the API route call them.
7. Enforce the agent-to-agent access policy (§6.4): default-deny, explicit allow-list by (from, to, scope).

Tests: config validation (valid config accepted; a config with an unknown key or wrong type rejected without crashing the app); hot-reload applying a valid change and rejecting an invalid one while keeping the app on the last-known-good config; agent-to-agent policy default-deny and explicit-allow behavior; each CLI command against a throwaway config file.

Acceptance: I can hand-edit config/tradingos.config.json while the app is running, see a valid change apply within a few seconds, see an invalid change get rejected with a clear error and no crash, and drive the same changes via the CLI.
```

---

## Phase 2 — Orchestration Engine

```
Implement the orchestration engine per Build Spec §7.3 (this phase does NOT include the actual 24 agents yet — build the engine against a couple of fake/stub capabilities so it's testable in isolation; real agents come in Phase 3).

1. Tables: organization_runs, organizational_plans, tasks, task_dependencies, result_artefacts, organizational_decisions, organizational_events, approval_requests.
2. Planner (src/orchestration/planner.py): given an objective string, calls an LLM (stub this with a deterministic fake planner for now — real LLM router comes in Phase 3) to produce a task graph, then deterministically validates: acyclic, capability-matched against a registry, fixed safety order (code_validation → compliance_check → backtesting → strategy_evaluation → risk_assessment) when those capabilities appear in a plan. Up to 3 attempts before cannot_plan.
3. Task engine (src/orchestration/task_engine.py): Postgres advisory-lock task claiming, thread-pool dispatch for an allowlisted set of "concurrency-safe" capabilities, per-task timeouts, a 5-minute sweep flagging runs stalled >900s.
4. Dependency resolver: tasks wait on typed artefact dependencies, wake on completion, marked unsatisfiable on permanent upstream failure.
5. Run control (pause/continue/retry/rerun) with race-safe conditional UPDATE...WHERE status=... transitions. Retry only for transient failures (define a transient-vs-permanent classification). Rerun always produces a fresh plan, never inherits run_type from the source run.
6. Crash recovery: on app boot, reap_incomplete_runs() re-enters any run left non-terminal by a prior crash.
7. Approvals (src/orchestration/approvals.py): an ApprovalRequest blocks a defined transition (for now, model it generically — the concrete "Backtesting→PaperTrading" transition is wired in Phase 4) until a human decides. This must be enforced at the status-transition function itself, not only at one API route — write a test that tries to skip it via a different code path and confirms it's still blocked.
8. Event bus (src/orchestration/events.py): one events.emit() call does a Postgres insert + Redis publish, with gap-free per-run sequencing (a monotonic per-run sequence number).
9. Conflict detection (src/orchestration/decisions.py): deterministic checks for structurally conflicting decisions (e.g., two proposals for the same artefact with contradictory verdicts) — build the mechanism generically since concrete conflict types (market-vs-sentiment, risk-vs-deployment) depend on agents not yet built.

Tests: planner validation (rejects a cyclic or unknown-capability graph), task engine concurrency (two workers can't claim the same task), dependency resolution ordering, run control race-safety (concurrent pause+retry doesn't corrupt state), crash recovery re-entering a run after a simulated process kill, approval-gate unbypassability.

Acceptance: I can POST an objective, see a task graph get planned and executed against stub capabilities, pause/resume/retry/rerun it, kill the process mid-run and see it recover on restart, and confirm an approval gate can't be routed around.
```

---

## Phase 3 — Agent Roster, LLM Router & Skill Registry

```
Implement the 24-agent roster and its supporting infrastructure per Build Spec §7.1–§7.2 and §16.

1. LLM router (src/agents/llm_router.py): multi-provider (Anthropic, OpenAI, Gemini, DeepSeek, local Ollama) with a configurable fallback chain read from the Agent Gateway config (Phase 1), no restart needed to change order. Per-provider failure tracking (last-failure timestamp, fallback flag).
2. Define the fixed 24-agent roster (Build Spec §7.1) as capability-registered entries — each agent has a stable id, department, and the capabilities it can execute. This roster is NOT user-creatable via the Gateway (Phase 1's CLI configures identity/routing/skills for these fixed agents, it doesn't add new ones) — enforce that at the code level (the agent registry is a fixed dict/enum, not a DB table rows can be inserted into arbitrarily).
3. LangGraph pipeline (src/agents/graph.py, src/agents/state.py): build the 13-node graph from Build Spec §7.2 with a strict, versioned Pydantic state object. Wire each node to a per-agent circuit breaker — a disabled agent's node logic must not execute; enforce this at graph-build time.
4. Persona/identity (extends agent_identities from Phase 1): name, emoji, avatar, theme, voice — cosmetic only, must not be able to alter the agent's underlying role/capability.
5. Prompt versioning (prompt_versions table): immutable versions, diff-gated activation, rollback.
6. Skill Registry (src/agents/tools/): start with the skill set from Build Spec §16 (market-data-read, option-chain-read, portfolio-status-read, code-format-lint, sandbox-dry-run, notification-send — notification-send can be a stub until Phase 12). Global + per-agent grants enforced at the point of skill execution (not just checked in the UI/API layer). No mechanism for loading a skill that isn't shipped in this repo.
7. Heartbeat (src/agents/scheduler.py or similar): configurable-interval, read-only self-check for eligible agents (CEO, Risk Manager by default). CRITICAL: write the heartbeat execution context so it literally has no import/reference to any order-placement or risk-limit-mutation function — this should be structurally impossible to misuse, not just permission-checked. Write a test that tries to call a mutating function from within a heartbeat context and confirms it's unavailable, not just denied.

Tests: LLM router failover (simulate a provider failure, confirm fallback), disabled-agent circuit breaker (confirm node logic doesn't run), skill grant enforcement at execution time (an ungranted skill call fails even if attempted directly, not just hidden in the UI), heartbeat's structural inability to mutate orders/risk limits.

Acceptance: I can run an objective through the full LangGraph pipeline against real (or a cheap/small model for dev) LLM calls, disable an agent and confirm its step is skipped, and enable a heartbeat and see it produce read-only alerts on a schedule.
```

---

## Phase 4 — Strategy Pipeline & Sandbox

```
Implement the strategy pipeline per Build Spec §9.

1. Tables: strategies (status as a DB-level CHECK constraint: Ideation/Coding/Backtesting/PaperTrading/LiveEligible/Live/Deprecated — do NOT leave this as a plain unconstrained string), strategy_versions, strategy_suggestions.
2. Code generation node: LLM produces a Python module implementing run_backtest(data, config) -> dict.
3. Static validation: AST ban-list (no os, subprocess, socket, eval, exec, dynamic imports outside an allowlist) + ruff lint, before anything reaches the sandbox.
4. Sandbox (src/engine/sandbox/): stand up gVisor or Firecracker microVM execution (pick one — document the choice and why in a code comment) with CPU/memory rlimits, no network access, a read-only mount of the data lake and a writable scratch dir only, and an execution timeout. Build a warm-pool of pre-initialized workers so repeated calls don't pay a cold-start cost every time.
5. Wire the Backtesting→PaperTrading transition to the Approval mechanism from Phase 2 — this is the concrete case that phase built generically for. Confirm (with a test) that no code path — including a hypothetical direct status-update — can move a strategy from Backtesting to PaperTrading without an approved ApprovalRequest.
6. Human suggestions flow: free-text improvement request → AI review verdict → optional regeneration, diff-gated like prompt versioning.
7. Options legs grounding (for F&O strategies): ground legs against a live/mock option chain, run the naked-options scan (stub the actual scan logic here if Phase 6 hasn't built it yet — wire the real one in Phase 6) before acceptance.

Tests: AST ban-list rejects banned imports/calls; sandbox actually blocks network and filesystem-outside-scratch (write an adversarial test that tries to escape and confirms it fails); strategy status transition is unbypassable; warm-pool reduces latency on repeated calls (a rough timing assertion is fine).

Acceptance: submitting an objective produces a generated strategy, it passes static validation, runs inside the sandbox and produces a result, and cannot reach Backtesting→PaperTrading without a human approval.
```

---

## Phase 5 — Backtesting & Optimization

```
Implement backtesting and optimization per Build Spec §10.

1. Backtest engine (src/engine/backtest/): vectorized single-symbol backtesting; never fabricate a metric — non-finite results return null, not 0 or a default.
2. Indian friction model: brokerage, STT, exchange charges, SEBI turnover fees, stamp duty, GST, plus dynamic ATR/volume-based slippage.
3. Corporate-action adjustment: back-adjust OHLCV for splits/bonuses; document (in code comments and a docstring) that dividends are explicitly out of scope for price adjustment — don't silently approximate this.
4. Data-freshness gate: refuse to backtest if the data lake lacks the prior trading day's data (this can stub against a fake/seeded data lake until Phase 10 builds the real ingestion pipelines).
5. Walk-Forward Optimization: rolling train/test windows, a strategy must show positive out-of-sample expectancy in every window to pass.
6. Monte Carlo: 10,000 trade-resampled paths, 95th-percentile Max Drawdown (not historical MaxDD) governs sizing, distributed via Temporal (stand up a single-node Temporal server in docker-compose for this).
7. Optuna hyperparameter sweep with fANOVA parameter-importance — real per-trial execution, no shortcutting.
8. Multi-run comparison: pairwise return-correlation matrix, null (never fabricated 0/1) when fewer than 10 overlapping days.

Tests: friction model against hand-calculated examples; freshness gate refuses stale data; walk-forward correctly fails a strategy that's negative in any single window; Monte Carlo's 95th-percentile calculation against a known distribution; comparison matrix's null-below-threshold behavior.

Acceptance: a strategy can be backtested with realistic Indian costs, walk-forward and Monte Carlo validated, and compared against another run.
```

---

## Phase 6 — Risk & Safety Layer

```
Implement the deterministic safety layer per Build Spec §8. This is the most important phase in the whole build — treat every threshold and behavior here as load-bearing.

1. Max Drawdown Kill Switch (src/engine/risk/kill_switch.py): latching, 15% default, requires explicit human reset, never self-clears. A SEPARATE Paper Kill Switch instance that never shares state with the live one.
2. WebSocket Latency Guard: self-clearing pause above a configurable threshold (default 100ms).
3. Compliance Checker: SEBI-style position limits + NSE-style circuit-filter band + naked-options scan, deterministic, independent of any LLM output. Build the interface so a real regulatory data feed can be swapped in later without a schema change, even though you're starting with a maintained reference table.
4. Naked-options scanner: every sold option leg must have a same-underlying OTM hedge — wire this into the options-legs-grounding stub from Phase 4.
5. Correlation constraint: rejects proposed strategies/positions correlated to Nifty 50 beyond 0.85 (configurable) — wire this into BOTH the pre-deployment check AND the live/paper per-tick risk gate (this is an explicit fix vs. a prior build where it was only pre-deployment — don't repeat that gap).
6. Volatility-adjusted position sizing.
7. Broker circuit breaker interface (concrete broker adapters come in Phase 8 — build the circuit-breaker logic here against a fake adapter): opens after 3 consecutive 5xx, 1-min cooldown, alert fan-out hook (stub the actual notification until Phase 12).
8. Go-Live Readiness Gate: ALL required — ≥30 trades, ≥21 calendar days, ≥10-day clean Shadow Mode streak, ≤20pp live/backtest win-rate divergence (all configurable).
9. Dual-control risk limits (src/api/routers/risk_limits.py): stage → confirm-by-a-different-user → apply, self-confirm returns 403. This is now the ONLY way infra.riskThresholdRefs values (from Phase 1) actually change.

Tests: kill switch never self-clears and correctly blocks new order intents (not just submission) once tripped; correlation constraint fires in both the pre-deployment and per-tick contexts; Go-Live gate correctly requires ALL four conditions (test each condition failing alone blocks eligibility); dual-control rejects self-confirmation and rejects a confirm from an insufficiently-privileged user.

Acceptance: I can trip the kill switch and confirm nothing generates new order intents anywhere in the system until a human resets it; I can walk a strategy through the Go-Live gate and see it correctly blocked until all four conditions hold; I can stage and confirm a risk-limit change with two different users and see self-confirm rejected.
```

---

## Phase 7 — Paper Trading Engine

```
Implement the fully autonomous paper trading engine per Build Spec §11.

1. Layer 1 (daily signal): re-runs the full backtest once/day, reads the entries/exits transition to generate BUY/SELL signals.
2. Layer 2 (intraday): tick-driven universal stop-loss + re-entry, fed by Redis ticks (stand up Redis in docker-compose; a tick publisher can poll a broker quote endpoint or, until Phase 8, a mock feed).
3. Depth-walked fill simulation against Level-2-style quotes, honestly partial-filling when depth is insufficient (don't silently assume full fills).
4. Position accounting: average-cost-basis ledger (document this as a deliberate simplification vs. FIFO lot matching).
5. Margin model: approximate F&O margin — document clearly that this is not a real SPAN calculation.
6. F&O multi-leg: no atomic multi-leg primitive — a partial fill across legs must be surfaced honestly, never silently retried or rolled back.
7. This entire layer runs autonomously — no human approval required per paper trade. Confirm this is wired to run continuously during market hours without manual triggering (APScheduler, IST-aware).

Tests: fill simulation partial-fill behavior under thin depth; position accounting against hand-worked examples; multi-leg partial-fill surfacing (not retried/rolled back); the daily/intraday layers running on schedule without manual intervention.

Acceptance: paper trading runs hands-off once started, correctly simulates realistic fills and accounting, and never requires a human click for a paper order.
```

---

## Phase 8 — Broker Integrations

```
Implement broker integrations per Build Spec §13.

1. BrokerAdapter interface (src/brokers/base.py) with Zerodha Kite Connect and Upstox implementations: place/modify/cancel order, order book, margin, positions, quote, option chain, expiries.
2. Wire the circuit breaker from Phase 6 to real adapters.
3. Shadow Mode: genuine sandbox dry-run calls where a sandbox exists (Upstox); honestly document (in code and in the audit trail) where it's local-payload-construction-only because no sandbox exists (Zerodha) — don't present these as equivalent anywhere in logs or UI-facing data.
4. Replace the Phase 7 mock tick feed with real broker-quote polling → Redis ticks.
5. Credential storage: encrypted local secrets store (Build Spec §3 — no Vault), never logged, write-only in any API response.

Tests: adapter contract tests against each broker's sandbox/mocked API; circuit breaker opens after 3 consecutive 5xx and respects the cooldown; Shadow Mode correctly differentiates real-sandbox vs. local-only per broker in its stored records.

Acceptance: paper trading now runs against real live broker quotes; Shadow Mode produces an honestly-differentiated confidence record per broker.
```

---

## Phase 9 — Live Order Intent Pipeline & Sign-off Queue

```
Implement the human-gated live trading flow per Build Spec §12 — this operationalizes the single most important product decision in this whole rebuild: full autonomy through paper trading, mandatory human validation for anything real-money.

1. live_order_intents table exactly as specified in §12.3.
2. LiveExecutionPipeline generates order intents automatically using the same tick-driven signal logic as the paper engine (Phase 7), but for a strategy that has been marked live-eligible (see step 4) — intents are written as status=pending_approval, never auto-submitted.
3. Expiry: a background process expires intents past their configured window (default 90s, hard platform-wide max 5 minutes) into status=expired — write this as a scheduled sweep, not a client-side timer, so it's correct even if no one is looking at the UI.
4. A one-time-per-strategy human sign-off ("approve strategy for live eligibility") required after the Go-Live Gate (Phase 6) passes, before the pipeline in step 2 will generate any intents for that strategy at all.
5. Approve/reject endpoints for individual intents; on approval, submit to the broker adapter (Phase 8) and write Order/Trade rows + an audit entry.
6. Bounded batch pre-authorization: a human can pre-approve up to N future intents for a strategy within a time window and a max-notional-per-intent cap — enforce the bounds server-side (reject an intent that would exceed the notional cap or the count, even if a batch authorization exists).
7. The Kill Switch (Phase 6), once tripped, must stop new intents from being GENERATED, not just block them at the approval step — verify this explicitly with a test.

Tests: an intent expires safely (never auto-submits) when unactioned past its window; an approved intent reaches the broker adapter and produces audit-logged Order/Trade rows; a rejected intent never reaches the broker; batch pre-authorization enforces its notional/count bounds server-side even against a client that tries to exceed them; a tripped kill switch stops intent generation entirely.

Acceptance: I can watch a live order intent appear, approve it and see a real (sandbox/paper-equivalent for testing) order placed, or let it expire and see it safely resolve to no-trade — and confirm the kill switch stops new intents from being created at all, not just from being approved.
```

---

## Phase 10 — Market Data & Data Lake

```
Implement market data pipelines per Build Spec §14.

1. DuckDB + Parquet data lake, partitioned year/month/symbol.parquet, with two nightly-refreshed catalog views (ohlcv_daily, ohlcv_intraday).
2. Scheduled incremental ingestion (daily, IST, skipping NSE holidays), corporate actions ingestion, instrument master sync — all SCHEDULED, not manual-only (an explicit fix vs. a prior build where these were manual/dormant).
3. Intraday minute-bar ingestion — also scheduled, not dormant.
4. NSE Bhavcopy / F&O bhavcopy as an on-demand fallback path.
5. Nightly data lake backup with checksum + row-count validation.
6. Market Pulse: India VIX, sector-index day-change, global index day-change, on-demand.
7. Now go back and remove the Phase 5 stub for the data-freshness gate — wire it to this real pipeline.

Tests: ingestion idempotency (running it twice doesn't duplicate/corrupt data); freshness gate correctly reflects real pipeline state; backup validation catches a deliberately corrupted test file.

Acceptance: the data lake populates and refreshes on its own schedule with no manual steps, and backtesting/paper trading now run against real, freshness-gated data end to end.
```

---

## Phase 11 — Audit & Observability

```
Implement audit logging and observability per Build Spec §19.

1. Hash-chained audit log (SHA-256), DB-trigger-enforced append-only (no UPDATE/DELETE possible through any code path — verify this at the DB level, not just the app level), advisory-lock-serialized writers.
2. WORM-style archive (MinIO with Object Lock, or a local append-only file store if you decide MinIO is overkill at this stage — document the choice) with periodic chain-divergence verification.
3. Export: CSV/NDJSON, filtered by entity/actor, role-restricted to SystemAdministrator/ReadOnlyAuditor.
4. Prometheus metrics: WS latency, agent node duration, order dispatch latency (against the documented budget), LLM token usage, trading-holiday gauge.
5. Grafana dashboard + at least the market-hours-downtime alert rule.
6. structlog with a correlation ID threaded from HTTP request → orchestration run → agent action → order — go back through Phases 0-10 and make sure this ID is actually propagated, not just present in isolated places.

Tests: an attempted UPDATE/DELETE against the audit_log table fails at the DB level; chain-divergence check correctly flags a tampered archive copy; export respects role restriction.

Acceptance: every mutating action from every previous phase now produces a verifiable, exportable audit entry, and I can trace one request end-to-end via its correlation ID in the logs.
```

---

## Phase 12 — Notifications & Omni-Channel

```
Implement notifications per Build Spec §18.

1. Telegram, Discord, Slack inbound webhooks with signature verification (secret-token / Ed25519 / HMAC-SHA256 respectively), replay-guarded, rate-limited.
2. Route verified-sender messages to the CEO Agent, with the option to spawn an organization run via message classification.
3. Outbound: the same channels for alerts (kill-switch trips, sign-off items — including live order intents from Phase 9, go-live gate passes). Every visual-only alert concept from the frontend must have a real outbound-notification equivalent here.
4. Wire the notification-send skill stub from Phase 3 to these real channels.
5. In-app chat: streaming responses, abort, per-session model switch, search, pin, export.

Tests: signature verification rejects a forged/replayed webhook; outbound alerts actually fire for each of the listed event types (kill-switch trip, sign-off item created, go-live gate pass).

Acceptance: I can trip the kill switch and get a real Telegram/Discord/Slack alert, and a live order intent appearing produces a real outbound notification, not just a UI badge.
```

---

## Phase 13 — Frontend Integration

```
The frontend already exists (built via v0.app, currently running on mock data, at frontend/). This phase wires it to the real backend — do not rebuild any UI from scratch.

1. Replace every mock-data module under frontend/lib/mock-data with real API calls / WebSocket subscriptions, using this mapping (from the frontend build's own handoff notes):
   - Activity feed, agent status → WebSocket channels agent-logs, organization-events, activity-feed
   - Live order intents + countdown → WebSocket channel sign-off-queue (server pushes expiresAt; the countdown ring must be driven off that server timestamp, NOT a client-started timer, so a page refresh never resets or extends the real expiry)
   - Ticks/order book → WebSocket channel ticks
   - Agent Gateway config screen → GET/PUT /api/v1/gateway/config, GET /api/v1/gateway/config/versions
   - Strategies, backtests, orders → REST under /api/v1/strategies, /api/v1/backtests, /api/v1/orders
   - Audit log → GET /api/v1/audit
   - Auth → POST /api/v1/auth/login, POST /api/v1/auth/refresh (no MFA step in the UI — remove any placeholder for one if v0 added it)
2. Implement the WebSocket channels server-side if any are still missing from earlier phases (some were built incidentally, e.g. order-events; the org-wide activity-feed and sign-off-queue channels are likely new).
3. Wire the mock 4-role switcher (built for dev preview in the frontend) to the real JWT-derived role, and make sure role-gated UI actually reflects real RBAC (a ReadOnlyAuditor should see the same restrictions in the UI as the API enforces).
4. End-to-end smoke test: log in, watch a real organization run appear in the activity feed and Kanban, approve a real strategy promotion, see a real live order intent countdown and let one expire, watch a real kill-switch trip turn the Organization Pulse orb red.

Acceptance: the premium UI now reflects real backend state everywhere — no remaining mock-data modules — and the countdown/expiry timing is server-authoritative.
```

---

## Phase 14 — Security & Testing Hardening Pass

```
Final hardening pass per Build Spec §21–22 before treating this as usable beyond your own dev machine.

1. Adversarial sandbox testing: attempt filesystem/network escape from generated strategy code against the real gVisor/Firecracker sandbox from Phase 4; fix anything that succeeds.
2. Mutation testing on Kill Switch, Compliance Checker, correlation constraint, live-intent expiry logic — not just line coverage, actual mutation-kill rate.
3. RBAC contract tests across every mutating endpoint added since Phase 0, including the Agent Gateway config/CLI-backed ones from Phase 1.
4. Confirm the Agent Gateway's config/CLI/Control UI surface binds to localhost or an internal network interface by default — write a test/check that fails CI if this default ever silently changes to 0.0.0.0.
5. Accessibility pass on the frontend: high-contrast/reduced-motion theme meets WCAG 2.1 AA; every color-coded state (risk, order status, agent status, skill health) also carries a text label or icon.
6. End-to-end (Cypress/Playwright) suite covering: full strategy lifecycle, sign-off queue (both strategy-promotion and live-intent expiry/approval paths), Kill Switch trip-and-reset, Agent Gateway config hot-reload with an intentionally invalid config.
7. Load smoke test: WebSocket fan-out under several simultaneous Console connections (low priority at solo-operator scale, but confirm it doesn't fall over).
8. Update CLAUDE.md's build-status checklist to mark all phases complete, and do a final read-through of the Non-Negotiable Rules section to confirm nothing built along the way violates one of them.

Acceptance: this is the point where you'd be comfortable connecting real (even small) capital behind the human-approval flow — treat any failure found in this phase as a blocker, not a follow-up.
```

---

*End of pack. If you deviate from a phase's spec because something in practice doesn't fit, note the deviation and why in that phase's commit message and in CLAUDE.md — future-you (and future Claude Code sessions) should be able to tell "designed differently on purpose" from "not built yet."*
