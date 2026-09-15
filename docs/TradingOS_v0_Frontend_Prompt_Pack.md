# TradingOS 2.0 — v0.app Frontend Build Prompt Pack

**How to use this document:** v0's own guidance is to plan complex builds progressively rather than one giant prompt — start with a foundation, then build feature-by-feature, iterating in the *same* project/chat so v0 keeps prior context (design tokens, file structure, mock data types) available to later prompts. This pack gives you **12 sequential prompts**. Paste Prompt 0 first in a new v0 project, let it finish, review the result, then paste Prompt 1, and so on — don't paste them all at once. If a prompt's result drifts from spec, follow up in the same chat with a short correction rather than restarting.

This is a **frontend-only, mock-data build** — there is no backend yet. Every prompt specifies realistic TypeScript mock data so v0 builds fully functional-looking screens now; §13 at the end gives the real API/WebSocket contracts to wire in later without restructuring components.

---

## Prompt 0 — Foundation, Tech Stack & Design System

```
Create the foundation for "TradingOS" — a premium, AI-native control console for an autonomous multi-agent algorithmic trading platform (Indian equity & F&O markets). This is a control room for supervising 24 AI agents that research, backtest, and paper-trade strategies autonomously, with humans approving anything that touches real money.

TECH STACK
- Next.js (App Router), TypeScript, Tailwind CSS, shadcn/ui components
- Framer Motion for all UI animation/transitions
- React Three Fiber + drei for a 3D visualization element (details in a later prompt)
- Recharts (or visx) for financial charts (equity curves, histograms, gauges)
- Zustand (or React Context) for lightweight client state (theme, power-save mode, sidebar state)
- All data in this phase comes from local TypeScript mock modules under /lib/mock-data — structure them as if they were API responses, so swapping in real fetch calls later is a drop-in change

DESIGN LANGUAGE — "AI Mission Control", premium and futuristic, NOT a generic SaaS admin dashboard
- Dark-first design (this is a trading floor / night-ops aesthetic), with a light mode as a secondary option
- Glassmorphism panels: translucent dark backgrounds (bg-black/40 backdrop-blur-xl), thin 1px borders with a subtle glow (border-white/10, hover:border-cyan-400/40), soft outer glow shadows on interactive/active panels
- Typography: a technical/monospace font (e.g., "JetBrains Mono" or "Space Mono") for numbers, tickers, timestamps, and status codes; a clean geometric sans (e.g., "Inter" or "Geist") for body/UI text
- Base layout: a fixed left icon-rail sidebar (collapsible to labeled), a top bar with a global search/command palette trigger (⌘K), a live health indicator (green pulse dot = OK, red = degraded), and current market-hours status (NSE/BSE, IST)
- Color system: implement as CSS custom properties / Tailwind theme extension so it can be swapped at runtime. Define SIX palettes, all built around a near-black base (#05070a):
  1. "Nominal" (default) — cyan/emerald accents, used for healthy/idle/profitable states
  2. "Caution" — amber accents, for near-threshold/warning states
  3. "Risk" — red/crimson accents, for kill-switch-tripped/risk-alert states (this one should feel urgent — sharper contrast, faster pulse animations)
  4. "Dark Neutral" — desaturated blue-gray, a calmer default for long monitoring sessions
  5. "Light" — a light-mode variant keeping the same glass-panel language with dark text
  6. "High Contrast" — WCAG AA-compliant, reduced-motion-friendly, for accessibility — no reliance on color alone to convey state (pair every color cue with an icon or label)
  Build a theme switcher (a row of 6 small color-swatch buttons) accessible from the top bar, persisting choice in localStorage. Also add a "Power Save Mode" toggle next to it that (when on) disables ambient particle/glow animations and drops non-essential motion — wire the toggle now even if most animations come in later prompts.
- Motion principles: panels fade+slide in on mount (staggered by ~40ms per item), live-updating numbers use a smooth count-up/count-down transition (not a jarring snap), status changes pulse once, hover states lift a panel slightly (translateY -2px) with the glow intensifying. Respect prefers-reduced-motion.

NAVIGATION (left sidebar icon rail, top to bottom)
- Overview (home/pulse icon)
- Mission Control (kanban icon)
- Agent Fleet (network/org-chart icon)
- Strategies (flask icon)
- Backtests (bar-chart icon)
- Orders & Trades (arrows-exchange icon)
- Market Analysis (activity/pulse icon)
- Audit Log (shield icon)
- Settings (gear icon) — includes Agent Gateway config, notification channels, skill marketplace
- User/account menu pinned at the bottom

Build the root layout, the sidebar, the top bar (with theme switcher, power-save toggle, health indicator, market-hours badge, and a user avatar menu), and a placeholder Overview page that just shows the layout working with a few glass-panel skeleton cards. Set up the mock-data folder structure with an empty index and TypeScript interfaces for: Agent, OrganizationRun, Task, ApprovalRequest, LiveOrderIntent, Strategy, BacktestResult, Order, Trade — I'll populate these with real mock records in the next prompts, but define the shapes now so later prompts stay consistent.
```

---

## Prompt 1 — Authentication

```
Add authentication screens to the TradingOS project we just started (keep the same design system — dark glass panels, cyan accent, monospace numbers).

- A centered login card on a subtly animated dark background (a slow-drifting particle field or gradient mesh behind the glass card — respect power-save mode by freezing it when that's on)
- Fields: email, password, a "Remember this device" checkbox, submit button with a loading spinner state
- NO multi-factor authentication step — this app does not use MFA, keep it to a single password step
- Show inline validation errors and a distinct error state for "invalid credentials" vs. "network/connectivity error"
- After a mocked successful login, redirect to the Overview page
- Include a simple role indicator concept: mock 4 roles — SystemAdministrator, PortfolioManager, RiskManager, ReadOnlyAuditor — and store the selected mock role in state so later screens can conditionally show/hide admin-only actions (e.g., ReadOnlyAuditor sees everything read-only, no action buttons). Add a small dev-only role switcher in the user menu so I can preview each role's view.
```

---

## Prompt 2 — Overview / "Organization Pulse"

```
Build the Overview page — the heart of the app's premium/futuristic feel.

CENTERPIECE: "Organization Pulse" — a 3D animated orb (React Three Fiber + drei), centered in the top portion of the page, roughly 300-400px, that visually represents the whole trading organization's current state:
- IDLE — a calm, slowly-rotating sphere, soft cyan glow, gentle ambient particles orbiting it
- RESEARCHING — the orb's surface shows more turbulent/flowing distortion (use a noise-displaced shader or animated wireframe), particle orbit speeds up, cyan-to-blue gradient
- EXECUTING (paper trading active) — particles form tighter, faster orbits, green accent
- AWAITING APPROVAL (a live order intent is pending human sign-off) — the orb pulses amber with a noticeable heartbeat rhythm, and a small badge/ring shows a countdown
- RISK ALERT (kill switch tripped or a risk breach) — the orb turns red, pulses rapidly and sharply, particles scatter outward — this state should be unmistakable even to someone glancing at the screen from across the room
Add a mock state selector (dev-only floating control) so I can preview all 5 states without wiring a backend.
Below/around the orb: a horizontal KPI strip in glass cards — Active Agents (e.g. "22/24"), Live Runs, Pending Sign-offs (badge that pulses if > 0), Today's Paper P&L, Kill Switch status (a clear ON/OFF pill, red when tripped), NSE/BSE market-hours countdown ("Market closes in 2h 14m" or "Market closed").

BELOW THE FOLD: a two-column layout —
Left: "Live Activity Feed" — a real-time-styled, auto-scrolling (newest on top, gentle slide-in) chronological list of agent actions across the whole organization (e.g., "Strategy Generator proposed NIFTY-IronCondor-v3", "Risk Manager flagged correlation breach", "Paper order filled: RELIANCE 50 qty"). Each item: agent avatar/emoji, timestamp (relative, e.g. "12s ago"), a short description, and a colored left-border indicating severity (info/success/warning/critical). Include a filter chip row (All / Orchestration / Risk / Trading / System).
Right: "System Vitals" panel — small sparkline/gauge tiles for: LLM provider health (7 providers as small status dots with the active one highlighted), token usage today (progress bar against a budget), order-dispatch latency (a small line chart, with the 50ms budget line marked), CPU/memory of the platform.

Populate all of this with realistic mock data (at least 20 activity feed items, plausible Indian-market symbols like RELIANCE, NIFTY, BANKNIFTY, HDFCBANK, TCS).
```

---

## Prompt 3 — Mission Control (Kanban + Sign-off Queue)

```
Build the Mission Control page — this is where the operator manages everything the agent organization is doing.

TOP: a tab or segmented control switching between "Kanban" and "Sign-off Queue" views (or show both stacked if it reads well — your call on layout, but Sign-off Queue should always be reachable in one click from anywhere in the app via a persistent badge/bell icon in the top bar, since it's time-sensitive).

KANBAN VIEW: columns — Inbox → Planning → In Progress → Backtesting/Review → Sign-off → Live/Done. Each card represents an organization run or task: title (e.g. "Research: NIFTY weekly options edge"), the agent(s) involved (small avatar stack), a status chip, elapsed time, and a mini progress indicator. Support drag-and-drop between columns visually (doesn't need to persist anywhere real yet — just smooth drag animation with Framer Motion). Clicking a card opens a right-side drawer with a task-graph/DAG mini-visualization (simple nodes+edges is fine), an activity log scoped to that run, and any related artefacts.

SIGN-OFF QUEUE VIEW — this is the critical human-in-the-loop surface, design it to feel urgent but not alarming:
- Two sub-sections: "Strategy Promotions" and "Live Order Intents"
- Strategy Promotion cards: strategy name, a compact backtest metric summary (Sharpe, Max Drawdown, Win Rate, number of trades), a code-diff preview toggle, and Approve / Reject / Request Revision buttons
- Live Order Intent cards (the highest-stakes item in this whole app): symbol, side (BUY/SELL badge, green/red), quantity, strategy source, generated timestamp, and a prominent circular countdown ring showing time remaining before auto-expiry (default 90 seconds — animate it ticking down in real time using mock timers), with the ring changing color as it runs low (cyan → amber under 30s → red under 10s). Approve and Reject buttons, plus a note that an un-approved intent expires safely into "no trade" — make this reassuring copy, not scary. Include a small "batch pre-authorize" affordance (e.g., "Pre-approve up to 3 intents for this strategy in the next 30 min, max ₹50,000 notional each") as a secondary, less prominent action.
Include empty states for both sub-sections ("No pending approvals — the organization is running clean") and mock 2-3 live order intents with genuinely running countdowns so the interaction is visible immediately.
```

---

## Prompt 4 — Agent Fleet

```
Build the Agent Fleet page — a visual hierarchy of the 24-agent organization.

TOP: an org-chart / hierarchy tree visualization — CEO Agent at the top, branching into 6 departments (Executive, Market Intelligence, Research, Quant, Risk & Governance, Portfolio, Operations), each branching into their agents. Use a clean node-and-connector tree (SVG or a library like react-flow is fine) rather than a plain list — nodes are small glass cards with the agent's emoji/icon, name, and a status dot (green = active/idle, amber = degraded/missing dependency, red = disabled, gray pulse = currently executing). Support pan/zoom on the tree. Clicking a node opens a right-side (or full) workspace panel for that agent.

MOCK AGENT ROSTER (use these 24, grouped by department):
Executive: CEO Agent, CEO Chat Interface
Market Intelligence: Market Analyst, News Agent, Sentiment Agent
Research: Strategy Generator, Options Strategy Agent
Quant: Python Code Generator, Python Validator, Backtesting Agent, Optimization Agent, Evaluator
Risk & Governance: Risk Manager, Compliance Agent, Audit Agent (mark this one as "cannot be disabled" — its disable toggle should be locked with a tooltip explaining why), Deployment Agent
Portfolio: Portfolio Manager Agent
Operations: Memory Agent, Data Ingestion Agent, Scheduler Agent, Notification Agent, Skill Registry Manager, Execution Agent, Paper Trading Engine

PER-AGENT WORKSPACE PANEL, tabs:
- Identity: editable display name, emoji picker, avatar upload placeholder, accent color/theme picker (ties to the department's color), optional voice/TTS profile dropdown — a live preview card shows how the agent appears in the activity feed as you edit
- Model & Routing: AUTO (uses the global provider fallback chain — show the 7-provider order as a draggable list) vs. CUSTOM (pick a specific provider/model) toggle
- Prompt Versions: a version history list with a "diff" view between two selected versions and an "Activate" button on non-active versions (with a confirmation step — this should feel deliberately weighty, not a casual toggle)
- Skills: a grid of skill chips this agent is granted, with add/revoke actions
- Heartbeat: an enable/disable toggle with an interval slider (5-30 min), and copy clarifying "Heartbeat can only observe and raise alerts — it cannot place, modify, or cancel any order"
- Enable/Disable: a clear toggle with a "reason required" text field when disabling

Populate with plausible mock data across a few agents (don't need full depth on all 24, but the CEO Agent, Risk Manager, and Strategy Generator should have rich example data since they're the most-viewed).
```

---

## Prompt 5 — Strategies

```
Build the Strategies page.

TOP: a Kanban-style lifecycle board — columns: Ideation → Coding → Backtesting → Paper Trading → Live Eligible → Live → Deprecated. Strategy cards show: name, instrument type badge (Equity/F&O), a tiny sparkline of paper P&L if applicable, and status age.

Clicking a strategy opens a full Strategy Review view with tabs:
- Logic: a plain-language summary of what the strategy does, generated-looking (e.g., "Enters long when 20-EMA crosses above 50-EMA with RSI > 55, exits on ATR-based trailing stop")
- Code: a read-only syntax-highlighted code panel (monospace font) showing the generated Python, with a "Suggest Improvement" text box below where a human can submit free-text feedback, and a mocked "AI Review Verdict" response card that appears after submitting
- Code Diff: side-by-side or inline diff view between two versions
- Options Legs (only shown for F&O strategies): a simple table of legs (buy/sell, strike, expiry, quantity) with a "naked options scan: PASS/FAIL" badge
- Go-Live Readiness: a 4-condition checklist with progress (trade count ≥30, elapsed days ≥21, clean Shadow Mode streak ≥10 days, live/backtest win-rate divergence ≤20pp) — each condition shows current value vs. threshold with a progress bar, and an overall "Eligible" / "Not Yet Eligible" banner

Populate with 6-8 mock strategies spread across different lifecycle stages, with realistic Indian-market strategy names (e.g., "NIFTY-Weekly-IronCondor", "BANKNIFTY-ORB-Breakout", "RELIANCE-MeanReversion-v2").
```

---

## Prompt 6 — Backtests

```
Build the Backtests page.

- A strategy selector + "Run Backtest" button (mocked — shows a progress state then results)
- A full metrics grid: Sharpe, Sortino, Calmar, Max Drawdown, CAGR, Win Rate, Profit Factor, Expectancy — displayed as clean stat tiles with the metric name, value, and a small trend indicator
- An equity curve chart (line chart) plotting the strategy against a Nifty 50 benchmark line, with a drawdown area chart beneath it sharing the same x-axis
- A trade ledger table (entry/exit time, symbol, side, P&L, holding duration) — sortable, paginated
- A Walk-Forward Optimization results table — one row per rolling window, showing in-sample vs out-of-sample expectancy, with a pass/fail indicator per window
- A Monte Carlo section: a histogram of the 10,000 simulated max-drawdown outcomes, with the 95th-percentile value marked with a vertical reference line and called out as the number that governs position sizing
- A "Compare Runs" mode: let the user select up to 6 backtest runs and see them overlaid on one equity-curve chart plus a small pairwise correlation matrix (heatmap-style grid)
- CSV export button (can be a no-op/mock for now)

Use realistic-looking mock numbers (Sharpe around 1.2-2.1, drawdowns 8-18%, etc. — nothing absurd) and Indian Rupee formatting (₹) for currency values throughout.
```

---

## Prompt 7 — Orders & Trades

```
Build the Orders & Trades page.

- A unified table of Paper + Live orders/trades with filter chips (Paper/Live/Both, status, symbol search) and columns: time, symbol, side, quantity, price, status, strategy source, latency (ms)
- A summary strip above the table: today's P&L by strategy (small horizontal bar list), open positions count, latency stat (p50/p95 vs the 50ms budget)
- A dedicated "Live Order Intents" panel/tab reusing the countdown-ring pattern from the Sign-off Queue (Prompt 3) but scoped to a full history view — including past intents that were approved, rejected, or expired, each with a clear outcome badge
- A manual order entry panel (for the human-triggered path): symbol search/autocomplete, side, quantity, order type, and for LIVE orders specifically, a distinct two-step confirm (a modal that requires typing the symbol again or a similar deliberate friction step) — make it visually and interactionally clear that placing a LIVE order is a heavier, more deliberate action than a PAPER order (different button color/weight, an explicit warning strip)

Populate with a healthy mix of mock paper and live orders/trades across a trading day, using Indian equity/F&O symbols.
```

---

## Prompt 8 — Market Analysis

```
Build the Market Analysis page with 5 tabs:
1. Pulse — India VIX gauge, NSE sector-index day-change grid (heatmap-style tiles, green/red intensity by magnitude), global index day-change strip (Dow, Nasdaq, Nikkei, etc.)
2. Technical Indicators — a symbol search, then SMA/EMA/RSI/ATR/Bollinger Bands/MACD displayed as an overlaid price chart with indicator toggles
3. Data Freshness — a status table of each tracked dataset (OHLCV daily, instrument master, corporate actions, news, index OHLCV) showing last-updated timestamp and a freshness badge (Fresh/Stale)
4. Provider Status — the 7 LLM providers and 2 broker connections as status cards (healthy/degraded/down, last failure time if any)
5. Live Option Chain — a symbol selector (F&O names) and an option chain table (strikes as rows, calls/puts as column groups, OI/IV/LTP), with the ATM strike highlighted

Use realistic mock data throughout, Indian market context (NSE/BSE symbols and sector names).
```

---

## Prompt 9 — Audit Log

```
Build the Audit Log page.
- A filterable, paginated table: timestamp, actor, action, entity type, entity id, before/after state (expandable row showing a JSON diff), and a hash-chain integrity indicator per row (a small checkmark icon meaning "verified in chain")
- Filters: date range, actor, entity type, action type
- Export buttons: CSV and NDJSON
- Restrict the visible actions in the UI based on the mock role from Prompt 1 — ReadOnlyAuditor and SystemAdministrator can view this page; other roles see an access-restricted state (design this state to look intentional, not like a broken page)
- A small "Chain Integrity" status strip at the top confirming the hash chain has no detected divergence, with a timestamp of the last verification run

Populate with 30-40 mock audit entries covering a mix of action types (order placed, risk limit changed, agent disabled, config updated, strategy approved).
```

---

## Prompt 10 — Settings (Agent Gateway, Skills, Notifications)

```
Build the Settings page with a left sub-nav and these sections:

1. Agent Gateway Config — a form-based editor mirroring a config file structure (infra defaults, agent defaults, per-agent overrides, channel bindings), with a "Raw JSON" toggle/tab that shows the equivalent JSON5 for advanced editing. Include a "Validate" button that shows inline schema errors (mock a couple of realistic ones like "unknown key: agents.entries.ceo-agent.foo"), and a config version history list with a "Rollback to this version" action per row.

2. Skill Marketplace — a searchable card grid of skills (market-data-read, option-chain-read, portfolio-status-read, code-format-lint, sandbox-dry-run, notification-send), each card showing a health dot (green/yellow/red), a short description, which agents currently have it granted (avatar stack), and an enable/disable toggle. Clicking a card opens a detail panel with a per-agent grant matrix (a simple checkbox grid: agents as rows, this skill as the column, though frame it as reusable for showing all skills × all agents too).

3. Broker & LLM Credentials — write-only-styled credential fields (masked, "•••• saved" state instead of showing values) for Zerodha, Upstox, and each of the 7 LLM providers, with a connection-status indicator per integration and a "Test Connection" button.

4. Notification Channels — Telegram/Discord/Slack connection cards with a connect/disconnect flow (mocked) and per-channel alert-level preferences (checkboxes: kill-switch trips, sign-off items, go-live gate passes, daily summary).

5. Risk Limits — current threshold values (max drawdown %, correlation limit, latency guard ms) shown read-only with a "Propose Change" flow that visibly requires a second, different user to confirm before it applies (mock this as a two-step UI: Stage → a distinct "Awaiting second approval" pending state → Confirmed), making the dual-control requirement visually obvious.

Keep the same glass-panel, dark, monospace-numbers aesthetic throughout.
```

---

## Prompt 11 — Global Polish Pass

```
Do a global polish pass across the whole app:
- Verify Power Save Mode (from Prompt 0) actually disables the Organization Pulse orb's particle effects and any ambient background animation everywhere, dropping to a static/simplified state, while keeping essential state-communicating motion (e.g., the sign-off countdown ring still ticks, just without extra glow/particle flourishes)
- Verify all 6 themes apply consistently across every page built so far, including chart colors (not just backgrounds/borders) — charts should re-theme too
- Add a command palette (⌘K) that can jump to any page, search strategies/orders/agents by name, and trigger quick actions (e.g., "View Sign-off Queue", "Toggle Power Save Mode")
- Full responsive pass down to ~390px width: the sidebar collapses to a bottom tab bar or a slide-out drawer on mobile, the Organization Pulse orb shrinks/simplifies on small screens, tables become horizontally scrollable within their own container (page itself never scrolls horizontally), and touch targets are comfortably sized
- Add a reduced-motion mode that automatically activates from the OS-level prefers-reduced-motion setting (independent of the manual Power Save toggle), removing all non-essential animation
- Do a pass ensuring every color-coded status anywhere in the app (risk states, order status, agent status, skill health) also carries a text label or icon — never color alone
```

---

## After v0: Wiring to the Real Backend

When the FastAPI backend from the Build Specification exists, these are the real contracts to swap the mock data for (no component restructuring should be needed if the mock TypeScript interfaces from Prompt 0 were followed):

| Mock data source | Real source |
|---|---|
| Activity feed, agent status | WebSocket channels: `agent-logs`, `organization-events`, `activity-feed` |
| Live order intents + countdown | WebSocket channel: `sign-off-queue` (pushes intents with server-computed `expiresAt`) |
| Order book / ticks | WebSocket channel: `ticks` |
| Agent Gateway config | `GET/PUT /api/v1/gateway/config`, `GET /api/v1/gateway/config/versions` |
| Strategies, backtests, orders | REST endpoints under `/api/v1/strategies`, `/api/v1/backtests`, `/api/v1/orders` |
| Audit log | `GET /api/v1/audit?filters=...` |
| Auth | `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh` (JWT access + refresh token, no MFA step) |

Keep the countdown-ring timer client-driven off a server-provided `expiresAt` timestamp (not a client-started timer), so a page refresh doesn't reset or extend a live order intent's real expiry.

---

### A note on v0 usage
Per v0's own documentation, complex builds go better broken into steps like this rather than one mega-prompt — plan → build → iterate, component by component. Sources consulted for this guidance:
- [What is v0? — v0 Docs](https://v0.app/docs)
- [v0 Prompt Engineering — v0 Docs](https://v0.app/docs/text-prompting)
