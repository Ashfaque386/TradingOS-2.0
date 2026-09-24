# Phase 19 — Mission Control, Agent Fleet & Roster Audit

Scope: compare this app against real OpenClaw-ecosystem mission-control
dashboards and real multi-agent trading-system architectures (the 18-agent
"AI Hedge Fund" reference design: value/growth/contrarian/macro investor
personas + quant specialists + a weighted-consensus Portfolio Manager), to
find real gaps before building anything. Three questions per area: (1) does
the backend capability exist at all, (2) if it exists, is it actually wired
to something real (not disconnected/inert), (3) is it surfaced in the
frontend. Every finding below is classified as **missing backend**,
**missing frontend surface for an existing backend capability**, or **both**.

Method: full reads of `app/mission-control/page.tsx` and
`app/agent-fleet/page.tsx`; traced every WebSocket channel
(`lib/ws.ts`, `backend/src/api/routes/websockets.py`) to its real publish
call sites; traced every scheduler module under `backend/src/orchestration/`;
traced the Phase 12 chat system end to end; traced Prompt Versions from the
UI through the API to the actual LLM-call prompt-construction code; read
`backend/src/agents/roster.py` (capabilities) and `backend/src/gateway/roster.py`
(the 24-agent roster) in full, cross-referenced against
`backend/src/agents/graph.py`'s compiled LangGraph pipeline; grepped the
whole backend for any fundamentals-shaped data field. No code was written
during this pass — audit only.

## Summary

| # | Finding | Classification |
|---|---|---|
| 1 | No live "what is an agent doing right now" feed | **Both** — no backend event source carries per-step reasoning detail, no frontend panel exists |
| 2 | 15 real scheduled jobs across 6 schedulers, none visible in any UI | **Missing frontend** — backend data (job registration + cadence) is real and enumerable |
| 3 | Operator can't leave guidance the next planning cycle reads | **Both** — chat backend exists but is architecturally isolated from the planner; zero frontend chat UI at all |
| 4 | Prompt Versions tab is real CRUD but has zero effect on real LLM calls | **Missing backend** — the disconnect is prompt-construction code never reading the `prompt_versions` table, not a UI problem |
| 5 | No per-agent activity/performance history anywhere | **Both** — no `agent_id` column on events, no call site attributes audit rows to a roster agent id; no frontend panel |
| 6 | Roster has no screening/fundamentals/valuation/macro/post-trade-review/investor-reporting agent | **Missing backend** — confirmed absent in both the capability roster and the compiled pipeline |
| 7 | No fundamentals data source anywhere in the codebase | **Missing backend, and a genuine new data dependency** — not partially available from any existing source, must not be fabricated |

---

## 1. Mission Control

### 1.1 Live activity/reasoning feed — missing (both)

`app/mission-control/page.tsx` (203 lines) is a Kanban board (`OrganizationRun[]`
polled every 8s via `listRuns()`, page.tsx:124-138) plus the sign-off queue.
Its `TaskDrawer` shows each task's terminal `status` string
(queued/running/succeeded/failed) and `last_error` — a stage list, not a
live trace. No streaming text, no "Agent X is reasoning about Y", no
tool-call log anywhere in the component.

Five WebSocket channels exist (`backend/src/api/routes/websockets.py`):
`/ws/activity-feed`, `/ws/agent-logs`, `/ws/organization-events`,
`/ws/sign-off-queue`, `/ws/ticks`. Mission Control only subscribes to two
(`organization-events`, `sign-off-queue`). What each actually publishes:

- `activity-feed` / `agent-logs` — both stream the `audit_log` table polled
  every 1.5s (`websockets.py:61,79-113`) — coarse "an action happened"
  rows (`actor`, `action`, `entity_type`, `entity_id`), not agent-internal
  state.
- `organization-events` — Redis pub/sub from `src.orchestration.events.emit()`
  (`events.py:38-84`). Every call site
  (`task_engine.py:104-178,312,344`, `run_control.py:52-184`,
  `recovery.py:40`) is a task/run lifecycle transition
  (`task.started`/`task.succeeded`/`run.paused`/etc.) with payloads limited
  to `{task_id, capability}` or similar — never LLM prompt/response text,
  current pipeline node, or tool-call detail.
- `sign-off-queue` / `ticks` — approvals and raw price ticks, unrelated.

The one piece of per-step data that exists at all,
`TradingOSGraphState.node_log: list[str]` (`agents/state.py:40`, appended to
by each real node, e.g. `graph.py:61`), is accumulated only for the
duration of one `ainvoke()` call and returned whole at the end by
`run_pipeline_endpoint` (`api/routes/agents.py:191-199`) — post-hoc, not
live, and structurally disconnected from `events.emit()`/`organization-events`
(`graph.py` never imports `src.orchestration.events`). Every LLM-backed node
calls `router.complete()` (single non-streaming completion,
`graph.py:45` `_llm_or_fallback`), not `router.stream_complete()` (which
does exist and is already used by chat — `chat.py`).

**Nothing in this codebase streams "agent X is currently reasoning about Y"
today.** Building this needs: (a) `events.emit()` calls inside `build_graph()`'s
per-node wrapper (`graph.py:214-223`, which already wraps every node for
timing — the natural insertion point) and (b) switching the LLM-backed nodes
to `stream_complete` for token-level detail.

### 1.2 Automation schedule visibility — missing frontend (backend data is real)

Six scheduler modules, 15 real jobs:

| Scheduler | Jobs | Cadence |
|---|---|---|
| `market_data_scheduler.py` | incremental daily, corporate actions, instrument master, intraday ingestion, catalog refresh, backup | Cron 18:00/18:05/18:10/22:00/22:15 IST daily + 300s interval |
| `paper_trading_scheduler.py` | daily signal, tick publish, tick drain | Cron 08:00 IST daily + 3s/5s intervals |
| `live_trading_scheduler.py` | daily signal, intent generation, expiry sweep, reconciliation | Cron 08:00 IST daily + 5s/10s/15s intervals |
| `audit_scheduler.py` | archival sweep, divergence check | 300s / 3600s intervals |
| `notification_scheduler.py` | daily summary | Cron 18:00 IST daily |
| `agents/scheduler.py` (heartbeat) | heartbeat sweep | Plain `asyncio` loop, 300s (not APScheduler) |

A repo-wide grep for `next run`/`cron`/`schedul` across every page returns
exactly one hit, and it's static help text
(`app/strategies/page.tsx:176`), not a schedule display. **No page anywhere
lists any of these 15 jobs, their cadence, or next-run time.** This is a
pure frontend gap — the backend already knows every job's id and trigger
(APScheduler's own `scheduler.get_jobs()` can enumerate them at runtime; a
new endpoint reading that is the only backend work needed).

### 1.3 Operator feedback/guidance the next planning cycle reads — missing (both)

The Phase 12 chat system (`backend/src/orchestration/chat.py`,
`backend/src/api/routes/chat.py`) is real on the backend — real sessions,
real streamed LLM replies via `stream_complete`, persistence, search/pin/export
(`chat.py:62-257`) — but:

- **Zero frontend consumer.** `grep` across `app/`, `components/`, and
  `lib/api.ts` for any chat-session client function, `/chat/` route, or chat
  UI component returns nothing. An operator cannot reach this feature
  through the product today.
- **Even wired up, it would not feed the planner.** The chat LLM call uses
  `agent_id="ceo-chat-interface"` (`chat.py:233`), a distinct identity from
  `"ceo-agent"` used by the real pipeline's kickoff node
  (`graph.py:57`). The real planning entry point,
  `create_plan(db, redis, *, objective: str, ...)` (`planner.py:273`), takes
  only the free-text objective string typed into Mission Control's
  "Give the organization a new objective…" box (`page.tsx:186`) — no
  session id, no chat history, no guidance table is read anywhere in
  `backend/src/orchestration/planner.py`. Chat is purely reactive Q&A: one
  message in, one reply out, never referenced again outside that session's
  own history.
- **No existing frontend-only stub either** — neither Mission Control nor
  the Overview page (`app/page.tsx`) has any free-text notes/guidance input;
  Mission Control's only text input is the one-shot objective box.

**Exact change needed**: `create_plan`/the real planner function needs a new
parameter that reads recent/pinned rows from a guidance source (new table,
or repurposed `ChatSession.pinned` — `chat.py:23,81`) and folds that content
into the objective before `graph.py`'s `ceo_kickoff` node runs. No such read
path exists anywhere today.

---

## 2. Agent Fleet

### 2.1 What each tab actually does (`app/agent-fleet/page.tsx:16`, 5 tabs)

| Tab | Real or stub | Evidence |
|---|---|---|
| Identity | **Real** | `PUT /api/v1/agents/{id}/identity` → `gateway/service.py:69-98` → `apply.py` (writes config + `AgentIdentity` + audit row). Minor gap: audit `actor` is hardcoded `"cli"` (`gateway/service.py:98`), not the authenticated user — `current_user` is fetched in the route but never passed through, so per-user attribution is lost. |
| Prompt Versions | **Real CRUD, but inert** — see 2.2 | |
| Skills | **Real** | Same `apply_config_text` path as Identity, via `PUT /gateway/config`. |
| Heartbeat | **Real** | Same gateway-config path; toggle persists. |
| Enable/Disable | **Real, and pipeline-effective** | `build_graph()` (`graph.py:197-226`) checks `enabled_agents` at build time and swaps a disabled agent's node for a skip-node — disabling a pipeline agent genuinely skips that step on the next run. |

Agent cards show only `display_name` + a static active/disabled dot
(`statusOf()`, `page.tsx:18-20`) — no last-run time, no result, no counts.

### 2.2 Prompt Versions — the single most important finding: missing backend wiring

The tab's create/list/activate actions are genuinely real HTTP + real
Postgres rows (`prompt_versions.py:33-108`), not a stub returning canned
data. **But activating a version has zero effect on what prompt is sent to
the LLM.** Every LLM-backed call site builds its prompt as a hardcoded
f-string and never touches the `prompt_versions` table:

- `graph.py`'s 4 LLM-backed nodes (`_node_ceo_kickoff:54-61`,
  `_node_strategy_generation:69-76`, `_node_code_generation:84-96`)
- `orchestration/strategies.py:141-146`
- `orchestration/strategy_suggestions.py:69-76`
- `notifications/inbound_router.py:93-99` (no system prompt at all — raw
  inbound text passed straight through)

`LlmRouter.complete(agent_id=…)` (`llm_router.py:494-545`) uses `agent_id`
purely as a logging/metrics label — it never looks up stored prompt
content. Neither `agents/roster.py` nor `gateway/roster.py` contains any
hardcoded persona/system-prompt text either — the roster is capability
metadata, not prompt content.

**Conclusion**: this is not a frontend problem. The UI, the API, and the
DB schema are all real and correct; the gap is that no code path anywhere
reads an agent's ACTIVE prompt version before making an LLM call. Fixing
this means changing the 4 `graph.py` nodes (+ the 2 orchestration call
sites) to look up the active prompt version for their `agent_id` and use it
as (or to build) the prompt, instead of the hardcoded f-string.

Also found: neither `create_prompt_version` nor `activate_prompt_version`
calls `write_audit_entry` — prompt version changes are the one Agent Fleet
mutation NOT captured in the hash-chained audit log, unlike every other tab.

### 2.3 Per-agent activity/performance history — missing (both)

- `OrganizationalEvent` (`models/organizational_event.py:11-37`) has no
  `agent_id` column at all — only `run_id, sequence, event_type, payload`.
- Audit log rows use free-form `entity_type`/`entity_id` per call site
  (e.g. `entity_type="agent_config_version"`, never `entity_type="agent"`
  keyed by roster agent id) — the query endpoint
  (`GET /api/v1/audit/entries?entity_type=&entity_id=&actor=`,
  `audit.py:71-85`) exists and works, but no data is ever written in a
  shape it could filter "by agent" against.
- No frontend panel exists to show this even if the data did.

Building this needs an `agent_id` column (or a capability→agent join layer,
since task payloads already carry `capability`, reverse-mappable via
`AGENT_CAPABILITIES` in `roster.py:25-50`) on events/audit, plus a new
panel.

---

## 3. Agent roster

### 3.1 Capability gaps — confirmed absent, backend work required

Full roster is 24 agents (`gateway/roster.py`), 13 of which map to real
LangGraph nodes via `PIPELINE_NODE_AGENTS` (`agents/roster.py:71-87`,
asserted length 13); the other 11 (chat interface, news/sentiment
scaffolds, audit/portfolio-manager/data-ingestion/scheduler/notification/
skill-registry/execution/paper-trading agents) operate through separate
subsystems or are pure capability metadata with no graph node.

Explicit answers, per the audit's required checks:

- **(a) Stock screening/filtering**: No. No capability, no node.
  `market-analyst`'s only wired logic is a hardcoded
  `{"trend":"neutral","confidence":0.5}` stub (`graph.py:64-66`), not real
  analysis, let alone screening.
- **(b) Fundamental/valuation analysis**: No. No capability, no node, no
  supporting data (see 3.2).
- **(c) Macroeconomic/calendar awareness**: No. No reference anywhere in
  `backend/src/agents` or `backend/src/orchestration`.
- **(d) Post-trade review of closed trades**: No. `memory-agent`, the
  closest-named candidate, only increments a strategy-rejection counter
  inside the generation retry loop (`graph.py:139-143`) — unrelated to
  closed live/paper trades.
- **(e) Investor-facing performance reporting**: No.
  `portfolio-manager-agent`'s only capability is read-only
  `portfolio-status-read` — no report-generation capability or node
  anywhere.

### 3.2 Fundamentals data — genuine new dependency, not fabricable

Grepped `backend/src/data/`, `backend/src/brokers/`, and the whole backend
for `fundamental|pe_ratio|eps|market_cap|book_value|revenue`: zero matches
anywhere. The one plausible candidate, `Instrument`
(`models/instrument.py:11-48`, synced from broker instrument masters), is
purely contract reference data — `symbol, exchange, instrument_type, isin,
lot_size, tick_size, underlying_symbol, expiry_date, strike_price,
option_type` — no P/E, EPS, market cap, revenue, or book value field of any
kind, and no other fundamentals-shaped table exists.

**For Part 2**: fundamentals is a genuine new data dependency with nothing
to partially reuse. Per this codebase's established "never fabricate a
metric" rule (the backtest engine's own convention), the Fundamentals and
Valuation agents cannot be built against real numbers without a new data
source this sandbox and this codebase do not currently have. Part 2's
Fundamentals/Valuation work must document this precisely rather than invent
plausible-looking numbers — see Part 2 plan for how this is handled
(an honest gap notice, matching the Market Analysis page's own established
pattern for exactly this situation, rather than a fabricated feature).
