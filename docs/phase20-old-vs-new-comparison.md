# Phase 20 — Old App vs. New App Comparison

Source inventories: `docs/phase20-legacy-audit.md` (old app, `D:\AI Trading Agent\TradingOS`) and this session's direct verification of the current app (`D:\TradingOS-2.0\TradingOS-2.0`) — 143 endpoints across 29 route files, 43 models, 12 frontend pages, 122 backend test files, cross-checked against `docs/CLAUDE.md`'s phase notes and its "Non-negotiable rules" and "Confirmed technical decisions" sections.

**Part 2 summary (CLAUDE.md cross-check).** Every CLAUDE.md claim spot-checked against real code this session held up: Phase 18's autonomous live-trading scheduler genuinely runs (`src/main.py::start_live_trading_scheduler`, not just built); the correlation constraint is genuinely wired into the real-time risk gate (`src/orchestration/risk_gate.py`), unlike the old app's same-named-but-unwired module; walk-forward, Monte Carlo, and Optuna-based hyperparameter sweep (with fANOVA importance) are all real and reachable via `/backtests/{walk-forward,monte-carlo,optimize,compare}`. No claim in CLAUDE.md was found unbacked by code. No significant real capability was found that CLAUDE.md fails to document — its phase-by-phase notes are consistently more detailed than the code itself requires. Three items (MFA, multi-tenancy, Vault) turn out to be **deliberately, explicitly excluded** by CLAUDE.md's own "Non-negotiable rules" (#4, MFA by name) and "Confirmed technical decisions" table (multi-tenant and Vault both explicitly ruled out for solo-operator scale) — this reclassifies them below from ordinary gaps into decisions the project has already made, not open questions.

**Classification key:** PRESENT (equivalent or better) · REGRESSED (old app did it, new app does it worse/not reliably) · MISSING (old app had it, no equivalent at all) · NEW (new app has it, old app never did).

---

## A. Where the new app is already ahead (NEW / PRESENT-superior)

1. **NEW — Autonomous live trading actually runs.** The old app's `LiveExecutionPipeline` is fully built and tested but **never instantiated by any worker** — its own live trading is human-triggered-only via the Orders API (`docs/phase20-legacy-audit.md`, prior-audit item 1). The new app's equivalent (`src/orchestration/live_trading_scheduler.py`, started in `src/main.py`'s lifespan) genuinely runs autonomously, gated by the Go-Live Readiness Gate + a per-strategy master switch + Kill Switch + standing caps + Compliance Checker + Correlation Constraint + Naked-Options Scanner, all deterministic (Non-Negotiable Rule #1). This is the single biggest capability gap in the old app's favor of a real trading operator, and the new app closes it.
2. **NEW — Correlation constraint is actually enforced live.** Old app's `correlation.py`/`position_sizing.py` are built and tested but not confirmed wired into the live/paper per-tick gate. New app's `evaluate_correlation_constraint` is directly called from `src/orchestration/risk_gate.py`, the real pre-trade path.
3. **NEW — Investor Reporting Agent.** Scheduled + on-demand generation of real investor reports (`src/api/routes/investor_reports.py`, Phase 19). Old app has no equivalent at all.
4. **NEW — Operator Guidance.** Standing operator instructions/context the agents read (`src/api/routes/operator_guidance.py`, Phase 19). Old app has no equivalent.
5. **NEW — Real broker OAuth login flow with an editable, always-visible redirect URL.** This session's own Phase 17 work: the callback URL is shown before any key exists (solving the broker's chicken-and-egg registration requirement) and is editable per broker. Old app's `broker_config.py` shows no evidence of a login-url/callback OAuth flow — credentials there appear hand-entered only.
6. **PRESENT (roughly equal) — Backtest analytics.** Walk-forward, Monte Carlo, Optuna sweep with fANOVA importance, and cross-run correlation matrix are all present and reachable (`/backtests/{walk-forward,monte-carlo,optimize,compare}`), matching the old app's `strategies.py` backtest-analysis surface feature-for-feature.
7. **PRESENT — Dataset freshness tracking.** `dataset_freshness_record.py` model exists in the new app; not missing as this audit initially assumed before checking code.
8. **PRESENT — Corporate actions.** `corporate_action.py` model exists in the new app.
9. **PRESENT (different shape) — Targeted rate limiting.** New app rate-limits specifically where it matters most for safety (`live_trading.py`, `webhooks.py`, notification senders/replay-guard) rather than generically across the whole API. See item 24 below for the general-API-rate-limiting gap this doesn't close.

---

## B. REGRESSED / MISSING items, grouped by domain

Each entry: what the old app did · why it matters to a real operator · effort (S/M/L) · new dependency, if any · overlap with in-flight phases.

### Auth & Access

10. **MISSING — Admin user management.** Old: list/create/deactivate users, `PATCH .../role`, revoke sessions (`users.py`). New: `admin.py` is a literal one-endpoint demo route whose own docstring says "Later phases' real admin endpoints (Agent Gateway config, user management, etc.) follow this same pattern" — the gap is self-documented as intentional-but-unbuilt. Matters because the new app's only way to create a non-first user is self-registration capped at ReadOnlyAuditor (`docs/CLAUDE.md`'s own note in `auth.py`), with no admin path to promote/deactivate anyone or force-logout a compromised account. Effort: **M**. No new dependency. No overlap with Phase 17b/18/19 — none of those touch user administration.
11. **MISSING — JWT signing-key rotation.** Old: `POST /jwt-signing-key/rotate`. New: no equivalent; rotating the JWT secret today means an env var change + full restart, invalidating every session with no graceful path. Effort: **S**. No overlap.
12. **DECIDED AGAINST, not a gap — MFA.** Old app has real, working TOTP MFA (Vault-backed secrets, backup codes) — but it was itself switched off for every role in the old app by explicit policy (`MFA_MANDATORY_ROLES = frozenset()`), so it wasn't actually protecting real logins there either. New app's `docs/CLAUDE.md` Non-Negotiable Rule #4 explicitly forbids adding it: *"No MFA... Don't add TOTP/MFA scaffolding even if a library makes it convenient."* Listed for completeness only — recommend leaving alone unless you want to revisit that rule itself, which is a different, bigger conversation than an ordinary feature gap.
13. **DECIDED AGAINST, not a gap — Multi-tenancy.** Old app has a real (if partially-wired) tenants table. New app's `docs/CLAUDE.md` states outright: *"self-hosted, not multi-tenant, not built for public/enterprise scale."* Not a gap to close; a scope boundary already drawn.
14. **DECIDED AGAINST, not a gap — HashiCorp Vault for secrets.** Old app runs real Vault (KV + Transit + unsealer). New app's `docs/CLAUDE.md` "Confirmed technical decisions" table: *"Secrets | Encrypted local secrets store (no Vault — dropped for solo-operator scale)."* Already decided; the Fernet-file store is the deliberate replacement, not an oversight.
15. **MISSING — Session/Casbin-style policy engine.** Old: real Casbin RBAC/ABAC (`policy.conf`/`policy.csv`). New: per-route `register_policy(method, path, roles=[...])` calls — simpler, works, but every new route's access rule lives in code next to the route rather than in one reviewable policy file. Low practical severity for a solo-operator, fixed-4-role system; noting for completeness. Effort if pursued: **L** (a real architectural swap, not a small addition). Not recommended given the current role model is small and static.

### Chat

16. **REGRESSED — No chat frontend page.** Old: a full `/chat` page. New: `ChatMessage`/`ChatSession` models and an LLM-router streaming path exist, but grep of `app/**/*.tsx` finds no dedicated chat page or route at all — the backend is real and unreachable through the console. Matters because "chat with the CEO agent" is a documented core interaction model for this kind of system and currently has no UI. Effort: **M** (backend already exists; this is a frontend build). No new dependency. No overlap with Phase 17b/18/19 as currently scoped, though it's exactly the kind of console-completeness work Phase 19 (Mission Control/Agent Fleet/roster upgrade) was already doing — worth folding into a Phase 19 follow-up rather than a standalone effort if Phase 19 work resumes.
17. **REGRESSED — Chat endpoint auth (old app's own bug, not a new-app comparison point).** Old app's `POST /chat/messages` has no auth dependency at all — a real hole in the old app. New app's chat/agent endpoints are properly RBAC-gated. Noting only so this isn't mistaken for something the new app needs to copy.

### Agents, Orchestration & Memory

18. **MISSING — Agent long-term/vector memory.** Old: real, multi-provider (Ollama/HuggingFace/local `sentence-transformers`/OpenAI/Gemini) embeddings into Qdrant, with query/ingest/collections endpoints, backing strategy/news/organization memory. New: `src/memory/__init__.py` is a one-line stub — `"""Qdrant collections and embeddings for agent memory. Implemented in Phase 3."""` — i.e. planned, never built, and the project's own phase numbering shows Phase 3 was superseded by the actual build order that followed. Matters: without this, every agent conversation/decision starts from zero context each run; no "have we seen this pattern before" recall across strategies, news, or organizational history. Effort: **L** — a new subsystem (Qdrant service + embeddings pipeline + retrieval wiring into the agent graph), not a small addition. New dependency: **Qdrant** (a new service in `docker-compose.yml`), plus an embedding provider choice (the old app's own documented experience — HuggingFace free tier rejects embedding models, Ollama was locally unreliable, local `sentence-transformers` was the resilient fallback — is directly reusable). No overlap with Phase 17b/18/19.
19. **MISSING — Live Canvas.** Old: one composed read endpoint answering "what's the newest real artifact of each kind right now" (generated strategy code, backtest result, agent log) across every strategy/run, backing a live side-by-side viewer. New: strategies, backtests, and agent logs are each viewed on separate pages with separate polling; no unified "what just happened, live" view. Effort: **M** (a genuinely simple composed read over data that already exists in the new app too — `strategy_versions`, `backtest_runs`, agent logs — the old app's own docstring emphasizes this computes nothing new). No new dependency. Natural fit if Phase 19's Mission Control work continues.
20. **MISSING — Scheduled jobs as a user-facing, editable system.** Old: `scheduled_jobs.py` + `ScheduledJob` model — list, detail, run-history, edit-schedule, run-now, for 12 registered jobs. New: jobs run via in-process APScheduler wired directly in `src/main.py`'s lifespan with no API or UI surface at all — an operator can't see job history, can't change a schedule, can't manually trigger one, without a code change and restart. Effort: **M**. No new dependency (APScheduler already in place; this wraps it with a DB-backed config + API). No overlap with existing phases.
21. **REGRESSED (partial) — Agent run lifecycle controls.** Old: pause/resume/cancel/**retry**/**rerun** on individual runs, plus analytics summary/trend charts and per-agent narration. New: has run tracking (`organization_run.py`, `task.py`, etc.) but this session did not find pause/resume/retry/rerun endpoints or trend-chart analytics in the routes reviewed — worth a closer, dedicated look before sizing, since it wasn't exhaustively checked. Effort: **M**, pending that closer look. Overlaps directly with Phase 19 (Mission Control/Agent Fleet) — should be folded into any resumed Phase 19 work, not built standalone.

### Portfolio & Risk

22. **MISSING — Portfolio allocation recommendations.** Old: a real LLM-driven advisory rebalancing workflow — the Portfolio Manager Agent generates recommendations from real strategy/backtest/broker-margin data, a human accepts/rejects with a full audit trail, explicitly advisory-only (no auto-execution path exists anywhere for it). New: no equivalent model, route, or agent output found. Matters: this is the one place the old app turns "many independent strategies" into "one coherent portfolio view with a recommendation," which a real operator running several strategies at once would likely want. Effort: **M** (the new app already has the Portfolio Manager agent role, real backtest data, and real broker margin queries — this composes existing pieces plus one new model/table and an accept/reject audit flow, not a from-scratch build). No new external dependency. No overlap with current phases — this would be new scope.
23. **MISSING — Cross-broker portfolio/margin dashboard.** Old: `/portfolio/margin/by-broker`, `/positions/by-broker`, `/portfolio/dashboard/summary`, `/portfolio/risk-metrics`. New: positions/PnL are visible per-mode (paper/live) via `orders.py`/`paper_trading.py`, but there's no single cross-broker margin/dashboard rollup. Lower priority than item 22 while the new app is still typically single-broker-at-a-time per strategy; worth revisiting once multiple live brokers are routinely connected simultaneously. Effort: **S–M**.
24. **MISSING — General API-wide rate limiting.** Old: `api_rate_limit.py`/`rate_limit_middleware.py`, generic across all endpoints, with a `/system/rate-limit/status` view. New: rate limiting exists only where it was deliberately added for a specific safety reason (live trading, webhooks, notifications) — there's no blanket protection against e.g. a compromised token hammering `/strategies` or `/backtests`. Effort: **S** (a FastAPI middleware, well-trodden ground) if this is judged worth it for a solo-operator, trusted-token deployment; genuinely optional given the threat model is narrower than the old app's presumed multi-tenant one.

### Compliance & Audit

25. **PRESENT, likely same honest limitation — Regulatory/compliance data.** Old app's SEBI/NSE compliance data is an explicitly-permanent 5-symbol hand-maintained placeholder, not a live feed. New app's `ReferenceTableRegulatoryDataProvider` was not re-opened in this pass to confirm its exact symbol coverage, but its name and this project's documented honesty posture (`docs/CLAUDE.md`'s repeated "honest gap, not fabricated" pattern) suggest a similarly-scoped placeholder rather than a live feed either. Not classified as a gap without a closer look — flagging as **worth a direct one-off check**, not a numbered decision item.
26. **MISSING — Dedicated audit trade-trace and actor-summary views.** Old: `GET /audit/trades/{entity_id}/trace` (full trace for one trade) and `/audit/actors/{actor_id}/summary` (per-actor rollup) as purpose-built response shapes. New: `/audit/entries` already supports `?entity_id=`/`?actor=` filters, so the *data* is one query away — there's just no purpose-built view or summary rollup (counts, first/last-seen, etc.) shaped for "show me everything about this trade" or "what has this operator done." Effort: **S** — thin, additive endpoints over data that already exists. No new dependency.
27. **MISSING — Continuous audit-chain monitor.** Old: `audit_chain_monitor.py` runs continuously as a background check. New: chain verification is on-demand only (`POST /audit/verify`), so a tampering event between two manual verify calls could go unnoticed for however long the gap is. Effort: **S** — wrap the existing verify logic in a scheduled job, same pattern as every other APScheduler job already in `main.py`. Natural fit if Phase 17b's env/ops-hardening work is still open (this is exactly the class of "silent gap only a live check catches" issue that phase has been finding).

### Deployment & Ops

28. **MISSING — CI test/security pipeline.** Before this session, the new app had **zero** CI. This session added `.github/workflows/ci.yml` (ruff, pytest with Postgres/Redis services, `tsc --noEmit`) — but the old app's pipeline additionally runs gitleaks (secret scanning), mypy strict, Bandit (SAST), pip-audit (dependency CVE scanning), applies real Alembic migrations, and gates on a per-module coverage threshold plus scoped mutation testing. Effort to close the remaining gap: **S** for gitleaks + pip-audit (drop-in actions), **M** for mypy-strict (the new app has never been type-checked this strictly — expect a real backlog of fixes) and a coverage gate, **M** for Bandit. Directly overlaps with Phase 17b (this session's own local-Docker/CI hardening work) — should extend the CI workflow just added rather than start a separate effort.
29. **MISSING — Frontend E2E in CI.** Old: a full Cypress job against a real seeded stack runs in CI. New: Playwright E2E specs exist (`e2e/*.spec.ts`, 4 files) but this session's new CI workflow does not run them — only `tsc --noEmit`. Effort: **S–M** (the specs already exist; this is wiring them into a CI job with a real backend+frontend+Postgres+Redis stack, similar to what this session already proved works for the backend job). Directly overlaps with Phase 17b/18's CI work from earlier in this session — extend, don't duplicate.
30. **MISSING — Docker image build-and-push to a registry.** Old: a dedicated `build-and-push` CI job to GHCR. New: images are built locally on demand only; nothing is published anywhere. Relevant only once there's an actual deployment target outside this machine. Effort: **S**. Optional until that's true.
31. **MISSING — Kubernetes/Helm.** Old: a real (if never-deployed-by-CI) Helm chart. New: `docs/CLAUDE.md` states deployment is "Docker Compose, self-hosted, no Kubernetes" — this is a stated scope decision, not an oversight, similar in spirit to items 12–14. Not recommended unless the deployment target actually changes.
32. **MISSING — Pre-commit hooks / local secret-scanning.** Old: `.pre-commit-config.yaml` + `.gitleaks.toml` at the repo root, catching problems before a commit is even made. New: none. Effort: **S**. Cheap, complements item 28 rather than substituting for it (pre-commit catches things locally before CI has to).
33. **PRESENT, informational parity — no frontend unit/component test layer in either app.** Both old (Cypress-only) and new (Playwright-only) rely on E2E as their sole frontend test tier. Not a regression either direction; noting because the old app's own prior audit flagged it as a real gap in that codebase too, and it's worth being aware the new app hasn't improved on this dimension, not that it's fallen behind.

### Market Data

34. **MISSING — Indicators overlay endpoint.** Old: `GET /ohlcv/{symbol}/indicators` — precomputed indicator series alongside OHLCV bars. New: indicator computation exists in the strategy/backtest engine but wasn't confirmed exposed as its own market-data endpoint for charting purposes outside a strategy run. Effort: **S**, if the underlying indicator functions are already reusable (likely, given `src/data/features` predecessor pattern in the old app and this project's general reuse discipline) — worth a direct check before sizing more precisely.
35. **MISSING — Datalake status/freshness as its own endpoint.** Old: `/market_data/datalake/status`. New: `dataset_freshness_record.py` exists as a model (item 7 above), but whether it's surfaced via a dedicated status endpoint wasn't confirmed in this pass. Effort: **S**, likely already close given the model exists — worth a direct check.

---

## Summary table

| # | Domain | Item | Class | Effort | Overlap |
|---|---|---|---|---|---|
| 1–9 | various | see Section A | NEW/PRESENT | — | — |
| 10 | Auth | Admin user management | MISSING | M | none |
| 11 | Auth | JWT signing-key rotation | MISSING | S | none |
| 12 | Auth | MFA | *decided against* | — | Rule #4 |
| 13 | Auth | Multi-tenancy | *decided against* | — | scope decision |
| 14 | Auth | Vault | *decided against* | — | scope decision |
| 15 | Auth | Casbin-style policy engine | MISSING | L | not recommended |
| 16 | Chat | Chat frontend page | REGRESSED | M | Phase 19 (if resumed) |
| 17 | Chat | (old app's own auth bug — not actionable here) | — | — | — |
| 18 | Agents/Memory | Vector/long-term agent memory | MISSING | L | none |
| 19 | Agents | Live Canvas | MISSING | M | Phase 19 (if resumed) |
| 20 | Agents | User-facing scheduled jobs | MISSING | M | none |
| 21 | Agents | Run pause/resume/retry/rerun + analytics | REGRESSED (unconfirmed depth) | M | Phase 19 (if resumed) |
| 22 | Portfolio | Allocation recommendations | MISSING | M | none |
| 23 | Portfolio | Cross-broker dashboard | MISSING | S–M | none |
| 24 | Ops | General API rate limiting | MISSING | S | none |
| 25 | Compliance | Regulatory data scope | needs a direct check | — | — |
| 26 | Audit | Trade-trace / actor-summary views | MISSING | S | none |
| 27 | Audit | Continuous chain monitor | MISSING | S | Phase 17b |
| 28 | CI | gitleaks/mypy/Bandit/pip-audit/coverage/mutation | MISSING | S–M | Phase 17b (extend) |
| 29 | CI | Playwright E2E in CI | MISSING | S–M | Phase 17b (extend) |
| 30 | CI | Image build-and-push | MISSING | S | optional |
| 31 | Deploy | Kubernetes/Helm | *decided against* | — | scope decision |
| 32 | CI | Pre-commit + local secret scan | MISSING | S | none |
| 33 | Frontend | (parity, not a gap) | — | — | — |
| 34 | Market data | Indicators overlay endpoint | MISSING | S | none |
| 35 | Market data | Datalake status endpoint | MISSING | S | none |
