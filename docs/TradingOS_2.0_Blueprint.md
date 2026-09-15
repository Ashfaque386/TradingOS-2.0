# TradingOS 2.0 — OpenClaw-Parity Feature & Requirements Blueprint

**Document type:** Forward-looking build blueprint for a from-scratch 2.0 rewrite.
**Baseline:** `TradingOS_Current_State_Features_and_Requirements.md` (reverse-engineered from the TradingOS 1.0 codebase, dated 2026-09-15).
**New research input:** Live web research on OpenClaw — the open-source autonomous agent project referenced as the feature/UX target — conducted 2026-09-14/15. OpenClaw sources are cited inline; every OpenClaw claim traces to one of them.
**Scope directive from the product owner:** Bring TradingOS's multi-agent control, configuration, and automation model up to OpenClaw parity, and rebuild the interface to be AI-oriented, futuristic, animated, and premium — but restrict all of it to the trading domain. TradingOS is not becoming a general personal assistant.

---

## 1. Executive Summary

TradingOS 1.0 is already a safety-first, audit-first, multi-agent trading system: 24 agents, a CEO-led planner, deterministic risk gates that sit above the LLM layer, a hash-chained audit log, and a working (if not-yet-autonomous) path to live capital. What it does **not** have is the kind of agent-operations experience that OpenClaw popularized in 2025–2026: a config-driven agent gateway you can reshape without redeploying, a CLI for standing up and rewiring agents in seconds, per-agent identity/persona, a marketplace-style skill system, proactive "heartbeat" behavior, and a mission-control-grade, real-time, visually alive console.

TradingOS 2.0 closes that gap in two coordinated tracks:

1. **Agent Gateway & Control Plane (backend/ops parity).** A new configuration and orchestration-control layer, modeled on OpenClaw's gateway/CLI/config system, that turns today's hard-coded 24-agent roster and its scattered settings screens into a single declarative config (`tradingos.json`-style) with hot reload, schema validation, a repair tool, and a CLI (`tradingos agents ...`) for adding, binding, teaming, and re-identifying agents — all still writing into TradingOS's existing Postgres-backed `agent_configs`/`prompt_versions`/`agent_control_state` tables, not replacing them.
2. **AI-Native Futuristic Console (frontend/UX parity).** A JARVIS-HUD-inspired console — a living 3D "Organization Pulse" visualization, glass/neon premium theming, real-time activity streams, a mission-control Kanban for tasks and human sign-offs, and an agent hierarchy view — replacing today's more conventional dashboard, built on top of TradingOS's existing five WebSocket streams rather than a UI rewrite from zero data.

**The one rule that governs every decision in this document**: nothing borrowed from OpenClaw is allowed to sit *below* or *bypass* TradingOS's existing deterministic safety layer (Kill Switch, Compliance Checker, Naked-Options Scanner, Go-Live Gate, Human Approval Gate). OpenClaw's own well-documented security incidents — broad tool permissions, exposed gateways, vulnerable third-party skills, an agent taking unauthorized real-world action — are treated in §8 as a checklist of failure modes to explicitly design against, because TradingOS's agents can eventually move real capital and OpenClaw's agents were never built with that constraint in mind.

---

## 2. What OpenClaw Actually Is — Research Findings

| Aspect | Finding | Source |
|---|---|---|
| Origin | Free, MIT-licensed autonomous AI agent created by Peter Steinberger (Austria); launched Nov 2025 as "Warelay," renamed "Moltbot" (Jan 2026), then "OpenClaw" days later; passed to an independent OpenClaw Foundation (backed by OpenAI) after Steinberger joined OpenAI in Feb 2026 | [Wikipedia](https://en.wikipedia.org/wiki/OpenClaw) |
| Growth | Over 214,000 GitHub stars by Feb 2026 — faster adoption than Docker, Kubernetes, or React | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |
| Core architecture | A local Node.js **Gateway** process routes messages from many channels to one or more **agents**; each agent runs an **agent loop** (up to 20 sequential tool calls/request); config is a JSON5 file (`openclaw.json`) that hot-reloads | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent), [Gateway Configuration](https://docs.openclaw.ai/gateway/configuration) |
| Multi-agent model | Each agent is a fully isolated persona: own workspace (`AGENTS.md`/`SOUL.md`/`USER.md`), own state dir (auth, session SQLite DB), own skill allowlist. Messages route via **bindings** (channel+account → agentId), matched most-specific-wins. Cross-agent session access is governed by an explicit allow-list, off by default for arbitrary pairs | [Multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent) |
| CLI | `openclaw agents list/add/delete/bind/unbind/bindings/team create/set-identity`, with `--role` templates (coordinator/researcher/writer/reviewer), `--bind`, `--model`, identity flags (`--name/--emoji/--avatar/--theme`) | [CLI: Agents](https://docs.openclaw.ai/cli/agents) |
| Configuration model | "Two-bucket rule": root-level keys = infra/cross-agent defaults; `agents.defaults` = agent-loop behavior; `agents.entries.*` = per-agent overrides of either bucket. Strict schema validation refuses to boot on bad config; `openclaw doctor --fix` repairs; a trusted last-known-good config is kept for rollback | [Gateway Configuration](https://docs.openclaw.ai/gateway/configuration) |
| Control UI | Local web dashboard (`http://127.0.0.1:18789`) — Overview (health + agent hierarchy tree), Channels, Instances, Sessions (model/context/tokens per conversation), Cron Jobs (incl. webhook mode), WebChat (streaming, abort, pin, search, export), Skills (color-coded health), Nodes, Config (form + raw JSON), Debug, Logs | [ClawdHost Dashboard Guide](https://clawdhost.net/blog/openclaw-dashboard-complete-guide/) |
| Automation | Cron jobs, webhook receivers, and a **Heartbeat** — proactive check every ~30 min with no user prompt needed | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |
| Skills / marketplace | 700+ community skills via the **ClawHub** registry; install without restart; skills are directories of metadata + instructions | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |
| Channels | WhatsApp, Telegram, Discord, Slack, Signal, iMessage, Microsoft Teams, Google Chat, Matrix, Zalo, browser chat — broadest channel coverage of any comparable tool | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |
| Mobile/remote control | No native mobile app — "mobile" is really "your existing Telegram/Discord app": receive progress pings, approve/deny actions, send new instructions mid-task | [Claude Code Channels vs OpenClaw](https://www.mindstudio.ai/blog/claude-code-channels-vs-openclaw-mobile-agent-control) |
| Third-party mission-control layer | **ClawControl** — a separate product providing a Kanban "Mission Board" (7 statuses/4 priorities), a human **sign-off queue**, a live agent activity feed over WebSocket, and one-dashboard agent provisioning with drift detection | [ClawControl](https://clawcontrol.dev/) |
| Community "premium UI" layer | **openclaw-jarvis-ui** — a HUD-style third-party dashboard: draggable 3D Three.js orb (IDLE/THINKING/RESPONDING states), 6-color HSL theme switcher, 3-tier audio visualization (spectrum/ring/waveform) reacting to TTS, live system monitor (CPU/mem/uptime/model/tokens via SSE), full CRUD task board, memory timeline (day-grouped), skills/schedule browser, power-save mode (60fps→15fps, disables particles), dual TTS engines (cloud + offline) | [openclaw-jarvis-ui](https://github.com/jincocodev/openclaw-jarvis-ui) |
| Model support | Claude (Opus/Sonnet/Haiku), OpenAI GPT, Gemini, DeepSeek, Moonshot, local via Ollama/vLLM | [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |
| Security track record | Cisco researchers found a third-party skill performing data exfiltration + prompt injection unknown to the user; an agent created an unauthorized dating profile on its own; researchers found 21,000+ exposed gateway instances leaking API keys/chat history; 26% of community skills contained vulnerabilities; multiple critical CVEs; China restricted state/government use (Mar 2026) | [Wikipedia](https://en.wikipedia.org/wiki/OpenClaw), [MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent) |

---

## 3. Feature Parity Map — OpenClaw → TradingOS 2.0

| OpenClaw capability | TradingOS 1.0 today | TradingOS 2.0 decision | Rationale |
|---|---|---|---|
| Gateway process + hot-reloading JSON5 config | Settings spread across DB rows (`agent_configs`), `routing.yaml`, env vars, no unified file, restart needed for some changes | **Adopt** — new `tradingos.json` config layer, hot reload, schema-validated | Directly closes an operability gap; §4.1 |
| CLI for agent lifecycle (add/bind/delete/team) | None — agents are fixed at 24, code-defined in `KNOWN_AGENTS` | **Adopt, scoped** — CLI for identity/routing/skill-grant changes only; agent *roster* stays curated (no free-form "add any agent"), since each of the 24 agents maps to a specific, safety-reviewed role in the LangGraph pipeline | Full OpenClaw-style arbitrary agent creation is inappropriate for a capital-affecting system; controlled config of existing roles is the safe analog; §4.1 |
| Per-agent workspace isolation (`SOUL.md`/`AGENTS.md`/`USER.md`) | Prompt versioning exists (`PromptVersion`) but no persona/identity layer | **Adopt** — per-agent identity file + `PromptVersion` extended with persona fields | §4.2 |
| Multi-agent routing via bindings | Fixed: capability → agent, planner-driven | **Not applicable as-is** — TradingOS routes by *capability*, not by *channel/account*; keep capability routing, but adopt the bindings *pattern* for the omni-channel layer (which Telegram/Discord/Slack chat message maps to which response persona) | Different problem shapes; only the channel-routing half transfers |
| Agent-to-agent session access control | Read-only `handoffs` view already exists | **Extend** — add an explicit allow-list config (which agents may read which other agents' artefacts/state), mirroring OpenClaw's `tools.agentToAgent.allow` | §4.1 |
| Skills system + ClawHub marketplace | Skill Registry: 13 skills, global + per-agent grants | **Adopt the UX, not the openness** — internal, curated "Strategy-Skill Marketplace" with health indicators and hot enable/disable; explicitly **no** open/public/third-party skill install | §4.3, §8 |
| Heartbeat (proactive, no-prompt-needed check) | 12 cron jobs exist, all schedule-driven | **Adopt, read-only only** — a heartbeat capability for the CEO/Risk Manager agents to self-review state between cron ticks; never permitted to place, modify, or cancel an order | §4.4, §8 |
| Cron + webhook automation | 12 cron jobs; Telegram/Discord/Slack webhooks in (not out) | **Extend** — generic outbound webhook mode (like OpenClaw's cron webhook trigger) for integrating future ops tooling | §4.4 |
| Control UI (Overview/Channels/Instances/Sessions/Cron/Skills/Config/Debug/Logs) | Console + Settings pages cover most of this piecemeal | **Adopt structure, trading-branded** — a unified "Ops" tab set inside the existing Console IA | §4.5 |
| Mission Board Kanban + sign-off queue (ClawControl) | Task DAG view + Approvals queue exist but are not Kanban-styled | **Adopt** — Kanban view over existing `tasks`/`ApprovalRequest` tables; sign-off queue becomes a first-class UI surface, not a buried approvals list | §4.5 |
| Live agent activity feed | Per-run activity stream exists; no org-wide feed | **Adopt** — org-wide chronological feed over the existing `organizational_events` bus | §4.5 |
| JARVIS-style 3D HUD, theming, audio visualization | None — conventional dashboard, light/dark toggle only | **Adopt, trading-tuned** — Organization Pulse orb, 6-theme system re-purposed around trading states (profit/caution/risk/kill-switch), optional audio alerts, power-save mode | §4.6 |
| Broadest channel coverage (11 channels) | 3 channels (Telegram, Discord, Slack) + dashboard chat | **Do not expand** — no WhatsApp/iMessage/Teams/Matrix/Zalo/Google Chat; each is attack surface with zero trading use case for a single-operator system | §4.7, §9 |
| Mobile control via chat app | Telegram/Discord bot commands already partially there via webhooks | **Extend** — richer outbound formatting for alerts, but still chat-native, no dedicated native mobile app in 2.0 scope | §4.7 |
| Multi-provider LLM routing | Already best-in-class: 7 providers with fallback (`llm_router.py`) | **Keep as-is** — TradingOS 1.0 already exceeds OpenClaw here | — |
| Public GitHub-star-driven open ecosystem | N/A | **Explicitly rejected** — TradingOS 2.0 is not open-sourcing its agent skill surface | §9 |

---

## 4. New / Upgraded Feature Set for TradingOS 2.0

### 4.1 Agent Gateway & Configuration Layer *(new)*

| Feature | Description | Backs onto (existing) | Notes |
|---|---|---|---|
| Unified config file | `tradingos.json` (JSON5), two-bucket rule: root = infra/cross-agent defaults (LLM routing, broker failover, risk thresholds pointers), `agents.defaults` = shared agent-loop behavior, `agents.entries.<agentId>` = per-agent overrides | `routing.yaml`, `agent_configs` table, env vars — consolidated, not replaced at the DB layer | File is the *editable surface*; DB remains system of record, config layer syncs to it |
| Hot reload | File-watcher applies non-destructive changes (identity, routing, skill grants, thresholds within policy) without restart | `main.py` lifespan | Capital-affecting parameters (risk limits) still require the existing dual-control stage→confirm flow — hot reload never bypasses it |
| Schema validation + repair | Boot refuses on invalid config; `tradingos doctor --fix` auto-repairs known-safe issues; last-known-good config retained for instant rollback | New | Mirrors OpenClaw's `openclaw doctor` |
| CLI: agent identity & routing | `tradingos agents list/set-identity/bind/unbind/bindings` — identity (name/emoji/avatar/theme), channel-persona bindings, skill grants | Extends `agent_settings.py` API with a CLI front-end | Roster itself (24 agents) stays fixed; CLI configures, does not create/delete core agents |
| Agent-to-agent access policy | Explicit allow-list of which agents may read which other agents' artefacts/handoffs, default-deny beyond the existing safety-ordered pipeline | Extends `handoffs.py` | Closes an implicit-trust gap OpenClaw itself only partially closes |
| Config audit trail | Every config change (via file, CLI, or UI) writes an audit-log entry like any other mutation | `src/core/audit.py` | Non-negotiable — OpenClaw has no equivalent, TradingOS must not regress its own bar |

### 4.2 Agent Identity & Persona Customization *(new)*

| Field | Purpose | Constraint |
|---|---|---|
| Display name, emoji, avatar | Cosmetic identity per agent, shown in Console/Chat/notifications | Cannot alter role/capability, only presentation |
| Color theme (per-agent accent) | Ties into the HUD's department color-coding (§4.6) | Drawn from the platform's approved palette only |
| Voice profile (TTS) | Optional distinct voice per agent for audio alerts | Off by default; opt-in per operator |
| Persona tone | Bounded stylistic instruction layered on top of, never replacing, the agent's `PromptVersion` safety/role instructions | Diff-gated activation, same as existing prompt versioning (Business Rule already in place) |

### 4.3 Strategy-Skill Marketplace *(upgrade of existing Skill Registry)*

| Feature | Description |
|---|---|
| Internal marketplace browser | Card/list view of all registered skills (start: existing 13) with health status (green = OK, yellow = missing dependency/credential, red = disabled/error) — visual pattern borrowed from OpenClaw's Control UI Skills panel |
| Hot enable/disable | No-restart toggling, already partially true (`SkillDisabledError`) — surfaced as a one-click UI action |
| Per-agent grant matrix | Visual grid: agents × skills, replacing the current settings-page list, for at-a-glance grant auditing |
| Versioned skill definitions | Each skill gets a version + changelog, consistent with the existing `PromptVersion` pattern |
| **No external/public skill install** | Deliberate deviation from ClawHub: all skills are authored and security-reviewed in-house | See §8 |

### 4.4 Automation: Heartbeat & Expanded Triggers *(upgrade)*

| Feature | Description | Guardrail |
|---|---|---|
| Heartbeat check | CEO Agent + Risk Manager run a lightweight, read-only self-review on a short interval (e.g., every 5–15 min during market hours) independent of the 12 existing cron jobs — flags anomalies (stale data, unexpected drawdown trend, provider degradation) into the activity feed/notifications | **Read-only**: may raise alerts and open a task for human review; may **never** place, modify, or cancel an order, or change a risk limit |
| Generic outbound webhook | Cron jobs (existing 12 + heartbeat) can optionally POST to an external URL on completion/failure, for ops tooling integration | Signed payloads, same HMAC pattern as inbound webhooks |
| Ad-hoc routing parity | Existing chat/webhook → `maybe_route_to_organization` classification stays; extended with persona-aware replies (uses the identity from §4.2) | — |

### 4.5 Mission Control Console *(new — restructures existing Console UI)*

| Feature | Description | Built over |
|---|---|---|
| Mission Kanban | Columns (Inbox → Planning → In-Progress → Backtesting/Review → Sign-off → Live/Done) over existing `OrganizationRun`/`Task`/`ApprovalRequest` state machine | Existing run/task model — no new state machine, just a Kanban *view* |
| Sign-off queue | Dedicated, always-visible queue of pending `ApprovalRequest`s with strategy diff, backtest metrics, and one-click approve/reject/request-revision | `approvals.py` |
| Org-wide activity feed | Real-time, chronological, filterable stream of all agent actions across all runs (not just per-run as today) | `organizational_events` bus (already emits via `events.emit()`) |
| Agent hierarchy tree | CEO → 6 departments → 24 agents, live/idle/disabled color-coded nodes, click-through to per-agent workspace | `agent_control_state`, existing per-agent settings pages |
| Unified WebChat | Streaming, abort, per-conversation model switch, search, pin, export — upgrades the existing CEO dashboard chat to Control-UI grade | `chat.py`, existing WebSocket infra |

### 4.6 JARVIS-Style Futuristic HUD *(new visual/interaction layer)*

| Element | Description | Trading-specific tuning |
|---|---|---|
| "Organization Pulse" orb | Central animated 3D visualization (WebGL/Three.js) reflecting overall system state | States: **Idle** (market closed / no active run), **Researching** (agents running), **Executing** (orders in flight), **Risk-Alert** (kill switch tripped / drawdown breach / circuit-breaker open) — color + particle intensity scale with state severity |
| 6-theme color system | One-click theme switching, HSL-based | Palettes reframed for trading semantics: cyan/green ("nominal/profit"), amber ("caution/near-threshold"), red ("risk/kill-switch"), plus neutral dark and light, and a high-contrast accessibility theme |
| Audio-reactive alerts | Spectrum/ring/waveform visualization tied to TTS voice alerts | **Opt-in, off by default** in a trading context — critical alerts must never rely solely on an animation; every visual alert is paired with a text/toast + existing Slack/Telegram/Discord fan-out |
| Live system monitor panel | CPU/mem/uptime, active LLM provider + token usage, order-dispatch latency | Surfaces existing Prometheus metrics (`tradingos_order_execution_latency_seconds`, HF usage gauge) directly in-console, not only in Grafana |
| Memory timeline | Day-grouped view of agent memory/decisions (TODAY/YESTERDAY, paginated) | Reads existing Qdrant `organization_memory`/`agent_memory` collections |
| Glass/neon premium visual system | Translucent panels, subtle glow borders, motion-designed transitions, dark-first with light mode | Applied consistently across Console, Settings, Strategy Review, Backtests, Orders |
| Power-save mode | Throttles animation (60fps→15fps), disables particles/glow | Persisted per-operator; recommended default **on** during active market hours to avoid distraction, **off** after-hours for review sessions |
| Mobile-responsive HUD | Touch gestures, swipe panels, collapsed orb view | Operator may check status from a phone intraday |

### 4.7 Omni-Channel — Deliberately Not Expanded

| Decision | Reason |
|---|---|
| Keep Telegram, Discord, Slack, dashboard chat only | These already cover verified, signature-checked inbound + outbound alerting; each additional channel (WhatsApp, iMessage, Teams, Matrix, Zalo, Google Chat) adds attack surface with no trading-specific benefit for a single-operator system |
| Improve formatting/streaming on existing channels | Matches OpenClaw's *quality* bar without inheriting its *breadth* |

---

## 5. Architecture Changes Required

```
[tradingos.json config file] ⇄ [Agent Gateway service]  (NEW)
        │  hot reload, schema validation, doctor/repair, audit-logged
        ▼
[Existing Orchestration]  src/orchestration/  ── unchanged state machine,
  run_manager / planner / task_engine / approvals / handoffs / events
        │  extended: agent-to-agent policy check, heartbeat trigger
        ▼
[Existing Agent Layer]  src/agents/  ── unchanged 24-agent roster & LangGraph
  graph, extended with: identity/persona fields, skill marketplace metadata
        │
        ▼
[Existing safety layer — UNTOUCHED]  Kill Switch / Compliance Checker /
  Naked-Options Scanner / Go-Live Gate / Human Approval Gate
        │
        ▼
[Existing execution/data/broker layers — UNCHANGED]
        │
        ▼
[NEW: Mission Control Console + JARVIS HUD]  frontend/
  subscribes to existing 5 WebSocket streams + org-wide event feed (extended);
  renders Kanban, sign-off queue, activity feed, agent tree, 3D orb, theming
```

Key principle: **the safety layer and execution/data/broker layers are not touched by this rewrite.** TradingOS 2.0 is an agent-operations and interface rebuild layered around an unchanged trading core — this keeps the rewrite bounded and avoids re-litigating already-hardened risk logic.

---

## 6. Requirements for TradingOS 2.0

### 6.1 Functional Requirements (new/changed — numbering continues the 1.0 document's scheme)

**Agent Gateway & Configuration**
- FR-GW-01: The system shall read agent, routing, and skill-grant configuration from a single hot-reloadable config file, validated against a schema before any change is applied.
- FR-GW-02: The system shall refuse to boot or apply a config change that fails schema validation, and shall retain the last-known-good configuration for automatic or manual rollback.
- FR-GW-03: The system shall provide a CLI for listing agents, setting per-agent identity (name/emoji/avatar/theme/voice), managing channel-persona bindings, and granting/revoking skills — without altering the fixed 24-agent capability roster.
- FR-GW-04: The system shall log every configuration change (file, CLI, or UI-originated) as an audit-log entry, including actor and before/after state.
- FR-GW-05: The system shall enforce an explicit allow-list for agent-to-agent artefact/state access, default-deny for any pair not listed.

**Persona & Identity**
- FR-ID-01: The system shall allow each agent a customizable display identity (name, emoji, avatar, theme, optional TTS voice) without modifying its underlying role prompt or safety instructions.
- FR-ID-02: Any change to an agent's persona-layer prompt content shall go through the existing diff-gated prompt versioning and activation flow.

**Skill Marketplace**
- FR-SKM-01: The system shall present all registered skills in a searchable internal catalog with per-skill health status (healthy / degraded / disabled) and per-agent grant visibility.
- FR-SKM-02: The system shall support enabling/disabling a skill globally or per agent without a service restart.
- FR-SKM-03: The system shall reject installation of any skill not authored and security-reviewed within the organization (no open marketplace ingestion).

**Automation**
- FR-AUTO-01: The system shall run a configurable-interval heartbeat check for the CEO and Risk Manager agents that may only read state and raise alerts/tasks, and shall be structurally incapable of invoking order placement, modification, or cancellation.
- FR-AUTO-02: The system shall support signed outbound webhook notifications on completion/failure of any scheduled job, including heartbeat-raised alerts.

**Mission Control Console**
- FR-CON-01: The system shall present all organization runs and their tasks in a Kanban view whose columns reflect the existing run/task state machine, without introducing new persisted states.
- FR-CON-02: The system shall present all pending approval requests in a dedicated, always-reachable sign-off queue showing strategy diff and backtest metrics inline.
- FR-CON-03: The system shall stream an organization-wide, filterable activity feed of agent actions in real time.
- FR-CON-04: The system shall render a live agent hierarchy tree reflecting each agent's enabled/disabled and active/idle status.

**Futuristic HUD**
- FR-HUD-01: The system shall render a persistent visual indicator of overall organization state (idle/researching/executing/risk-alert) that is driven by real backend state, not a decorative animation independent of system status.
- FR-HUD-02: Every visual-only alert (color change, animation, audio cue) shall be accompanied by an equivalent text notification through at least one existing channel (dashboard toast, Telegram/Discord/Slack), so no alert is visual-only.
- FR-HUD-03: The system shall allow the operator to switch color themes and toggle power-save mode, persisting the choice per operator.
- FR-HUD-04: The system shall surface live platform resource, LLM-provider, and latency metrics directly in the console, sourced from the existing metrics pipeline.

### 6.2 Non-Functional Requirements (new/changed)

- NFR-2.0-01 (Safety precedence): No feature introduced by this document may alter, weaken, or provide an alternate path around the Kill Switch, Compliance Checker, Naked-Options Scanner, Go-Live Gate, or Human Approval Gate.
- NFR-2.0-02 (Config safety): Hot-reloaded configuration changes affecting risk thresholds shall still require the existing dual-control stage→confirm workflow; hot reload applies only to non-capital-affecting settings (identity, routing, skill grants, UI theme).
- NFR-2.0-03 (Gateway exposure): The Agent Gateway's config/CLI/Control UI surface shall bind to localhost or an authenticated internal network path by default; it shall never be exposed to the public internet without mandatory MFA and audit logging enabled (re-enabling `MFA_MANDATORY_ROLES` is a prerequisite, not optional, for 2.0 — see §8).
- NFR-2.0-04 (Skill sandboxing): All skills, including new marketplace-cataloged ones, shall execute under the existing (or a hardened, container-grade) sandbox — never with raw shell or filesystem access to the host.
- NFR-2.0-05 (Performance): HUD animation and real-time visualization shall not add more than a small, bounded overhead to existing WebSocket stream latency budgets (NFR-01 in the 1.0 document); power-save mode shall be available as a mitigation.
- NFR-2.0-06 (Accessibility): The premium visual theme shall include a high-contrast/reduced-motion mode meeting WCAG 2.1 AA, since color-coded risk states must remain distinguishable to colorblind operators.
- NFR-2.0-07 (Auditability parity): Every new mutating surface (config CLI, identity editor, skill grant toggle, Kanban drag-drop transitions where they trigger real state changes) shall write to the existing hash-chained audit log — no new mutation path may bypass it.
- NFR-2.0-08 (Backward data compatibility): All new features shall be additive to the existing 45-table schema; no destructive migration is permitted outside a migration's `downgrade()`, consistent with the 1.0 document's NFR-08.

---

## 7. Full Feature Inventory — TradingOS 2.0 (Carried Forward + New)

| Area | 1.0 status | 2.0 treatment |
|---|---|---|
| Auth, sessions, RBAC, dual-control | Complete, MFA built but disabled | **Carry forward + harden**: re-enable MFA for SystemAdministrator/PortfolioManager/RiskManager as part of 2.0 (§8) |
| CEO-led orchestration console | Complete | **Carry forward**, restructured into Mission Control Console (§4.5) |
| Agent roster & LLM routing | Complete, 7-provider failover | **Carry forward unchanged**, wrapped by new Agent Gateway config layer (§4.1) |
| Strategy pipeline & sandbox | Complete; sandbox explicitly non-container-grade | **Carry forward + upgrade sandbox to container/VM isolation** (§8) |
| Backtesting & optimization | Complete | **Carry forward unchanged** |
| Risk management | Complete; correlation/position-sizing not wired into live per-tick gate (1.0 Gap #2) | **Carry forward + close Gap #2** as part of 2.0 hardening |
| Paper trading engine | Complete | **Carry forward unchanged** |
| Live trading & order management | Built but `LiveExecutionPipeline` never instantiated (1.0 Gap #1) | **Decision required from product owner**: 2.0 either formally wires the autonomous live pipeline behind the existing Go-Live Gate, or formally re-scopes live trading as human-triggered-only going forward — recommend resolving this before 2.0 build starts (see §10) |
| Broker integrations | Complete (Zerodha/Upstox) | **Carry forward unchanged** |
| Market data & data lake | Complete; instrument sync & intraday minute-bar ingestion unscheduled (1.0 Gaps #10) | **Carry forward + close scheduling gaps** |
| Chat, omni-channel, notifications | Complete; `POST /chat/messages` has no auth (1.0 Gap #8) | **Carry forward + fix Gap #8 as a blocking pre-2.0 item** |
| Audit, compliance, observability | Complete | **Carry forward unchanged**, surfaced live in HUD (§4.6) |
| Settings, scheduling, frontend shell | Complete | **Replaced/absorbed** by Agent Gateway config (§4.1) + Mission Control Console (§4.5) |
| **Agent Gateway & config layer** | — | **New** (§4.1) |
| **Agent identity/persona** | — | **New** (§4.2) |
| **Strategy-Skill marketplace** | Existing 13-skill registry | **New UX layer** (§4.3) |
| **Heartbeat automation** | — | **New** (§4.4) |
| **Mission Kanban + sign-off queue** | Existing approvals/task views | **New UX layer** (§4.5) |
| **Org-wide activity feed** | Per-run only | **New** (§4.5) |
| **JARVIS-style HUD & premium theme** | — | **New** (§4.6) |

---

## 8. Security Hardening — What NOT to Inherit From OpenClaw

| OpenClaw incident/weakness | Risk if replicated in a capital-affecting system | TradingOS 2.0 mitigation |
|---|---|---|
| Broad tool permissions (shell, filesystem, browser automation) available to agents | An LLM-driven agent with shell/filesystem access adjacent to a trading system is a direct path to capital loss or data exfiltration | Agents get only vetted Skill Registry capabilities (§4.3); no raw shell/filesystem/browser-automation tool is ever exposed to an agent |
| Third-party skill performed silent data exfiltration + prompt injection (Cisco finding) | A compromised or malicious skill could leak broker credentials, strategy IP, or trigger unintended actions | No open marketplace ingestion (FR-SKM-03); every skill is authored and reviewed in-house; skills run sandboxed (NFR-2.0-04) |
| 21,000+ exposed gateway instances leaking API keys/chat history | An internet-exposed Agent Gateway/Control UI is a direct credential-theft vector | Gateway binds to localhost/internal network by default (NFR-2.0-03); MFA re-enabled and mandatory for privileged roles |
| 26% of community skills contained vulnerabilities | Same class of risk as above, at the marketplace-scale | Internal-only skill catalog with a review gate before any skill reaches "enabled" status |
| Agent took unauthorized real-world action (unrequested dating profile) | An autonomous trading agent taking an unrequested capital-affecting action is the platform's single worst-case scenario | Heartbeat/proactive behavior is read-only-only by construction (FR-AUTO-01); Human Approval Gate remains mandatory and cannot be bypassed by any new automation path (NFR-2.0-01) |
| Prompt injection via processed inbound content | News/sentiment ingestion or chat/webhook content could smuggle instructions to agents | Extend existing webhook signature verification and replay guards to also apply content-level prompt-injection screening before news/sentiment content reaches any LLM-backed agent |
| Some jurisdictions restricted OpenClaw for government/enterprise use over security posture | Reputational/regulatory exposure if TradingOS's own agent layer is perceived as similarly loose | Treat OpenClaw strictly as UX/config-model inspiration, never as an embedded dependency or code import; document this hardened posture for any future compliance review |

**Two items from the 1.0 gap list become pre-2.0 blockers given this section:** re-enabling MFA (1.0 Gap #7) and fixing the unauthenticated `POST /chat/messages` endpoint (1.0 Gap #8) should both close *before* the Agent Gateway's expanded config/CLI surface goes live, since that surface increases what an unauthenticated actor could otherwise reach.

---

## 9. Explicitly Out of Scope for 2.0

- Any personal-assistant capability with no trading use case: email/calendar management, smart-home control, general web browsing/purchase-negotiation skills, generic "morning briefing" content.
- Additional messaging channels beyond Telegram/Discord/Slack/dashboard chat (WhatsApp, iMessage, Microsoft Teams, Google Chat, Matrix, Zalo, Signal).
- An open or public skill marketplace, or any mechanism for installing unreviewed third-party skills.
- Free-form agent creation/deletion by end users — the 24-agent roster remains a curated, safety-reviewed set; the Agent Gateway configures identity/routing/grants, not the roster itself.
- Native mobile app development (mobile access remains via existing chat channels and a responsive web HUD).
- Any weakening, bypass, or "fast path" around the Kill Switch, Compliance Checker, Go-Live Gate, or Human Approval Gate, regardless of how it might be framed as an efficiency gain.

---

## 10. Assumptions & Open Questions

1. **Live pipeline decision (blocking).** This document assumes the product owner will decide, before 2.0 build starts, whether `LiveExecutionPipeline` becomes a formally wired autonomous worker (behind the Go-Live Gate) or whether live trading is permanently re-scoped as human-triggered-only. The Agent Gateway and Heartbeat designs both change materially depending on this answer.
2. **MFA re-enablement timing.** Assumed to happen as part of, or immediately before, 2.0 rather than remaining deferred — flagged as a hardening prerequisite in §8, not automatically part of the original 1.0 backlog.
3. **Sandbox upgrade scope.** Assumed the existing subprocess-based strategy sandbox is upgraded to container/VM-grade isolation as part of 2.0, since the Skill Marketplace and Agent Gateway both expand the config surface around it; the exact isolation technology (gVisor, Firecracker, container runtime) is left to the engineering team.
4. **HUD technology choice.** Three.js/WebGL is assumed as the rendering approach (matching the OpenClaw community HUD precedent); final choice should weigh bundle size and mobile GPU performance.
5. **Voice/TTS necessity.** Audio-reactive alerting is scoped as strictly opt-in given the trading-floor context; if the product owner wants it default-on, NFR-2.0-06 (accessibility) and the "every visual alert has a text equivalent" rule (FR-HUD-02) still apply.
6. **Migration strategy.** This document does not prescribe a big-bang rewrite vs. parallel-run cutover; §8's blocking items and the live-pipeline decision (Q1) should be resolved first, after which a migration/rollout plan can be sequenced.

---

## 11. Suggested Build Sequence

| Phase | Focus | Depends on |
|---|---|---|
| A | Close pre-2.0 security blockers: re-enable MFA, fix unauthenticated chat endpoint, resolve live-pipeline decision (§10 Q1) | None — do first |
| B | Agent Gateway & configuration layer (§4.1), config audit trail | Phase A |
| C | Agent identity/persona (§4.2) + Strategy-Skill Marketplace UX (§4.3) | Phase B |
| D | Heartbeat automation, read-only only (§4.4) | Phase B |
| E | Mission Control Console: Kanban, sign-off queue, activity feed, agent tree (§4.5) | Phase B |
| F | JARVIS-style HUD, theming, live metrics panel, memory timeline (§4.6) | Phase E |
| G | Sandbox hardening to container/VM-grade isolation (§8) | Can run parallel to C–F |
| H | End-to-end security review against §8's checklist before any production exposure of the new Gateway/Control UI surface | After F and G |

---

## 12. Appendix — Glossary Additions for 2.0

| Term | Meaning |
|---|---|
| Agent Gateway | The new config-driven control-plane service managing agent identity, routing, and skill grants, modeled on OpenClaw's gateway |
| Bindings | Channel/account-to-persona routing rules, adapted from OpenClaw for TradingOS's omni-channel layer |
| Heartbeat | A proactive, read-only periodic self-check run by the CEO/Risk Manager agents, independent of cron |
| Mission Kanban | The Kanban-style view over existing run/task/approval state, inspired by ClawControl's Mission Board |
| Organization Pulse | The central live visualization reflecting overall system state (idle/researching/executing/risk-alert) |
| Strategy-Skill Marketplace | The internal, curated catalog UX for TradingOS's Skill Registry, styled after (but not open like) ClawHub |
| Power-save mode | A UI setting that reduces animation/particle rendering for performance or focus |

### Sources consulted
- [OpenClaw — Wikipedia](https://en.wikipedia.org/wiki/OpenClaw)
- [What Is OpenClaw? — MindStudio](https://www.mindstudio.ai/blog/what-is-openclaw-ai-agent)
- [OpenClaw Multi-agent routing — official docs](https://docs.openclaw.ai/concepts/multi-agent)
- [OpenClaw CLI: Agents — official docs](https://docs.openclaw.ai/cli/agents)
- [OpenClaw Gateway Configuration — official docs](https://docs.openclaw.ai/gateway/configuration)
- [ClawControl — Mission Control for OpenClaw Agents](https://clawcontrol.dev/)
- [openclaw-jarvis-ui (GitHub)](https://github.com/jincocodev/openclaw-jarvis-ui)
- [OpenClaw Dashboard: Complete Guide to the Control UI — ClawdHost](https://clawdhost.net/blog/openclaw-dashboard-complete-guide/)
- [Claude Code Channels vs OpenClaw mobile agent control — MindStudio](https://www.mindstudio.ai/blog/claude-code-channels-vs-openclaw-mobile-agent-control)

*End of document. This blueprint is a planning artifact, not an implementation spec — each numbered feature/requirement should be broken into engineering tickets during Phase-A/B kickoff, and the two blocking open questions in §10 should be resolved before detailed design work begins.*
