# Gap analysis — 2026-10-30

A read of the whole app against its own plans and docs, to say what is still pending and where
the gaps are. Point-in-time: it describes the code at commit `3bed570`. Each gap names the file
that shows it, so it can be checked and, when closed, struck out here.

**How it was done, and its limits.** Read from the code: routes, schedulers, models, config,
compose, CI, the frontend pages and the E2E specs, plus the earlier audits
(`phase16-wiring-audit.md`, `phase19-audit.md`, `phase20-old-vs-new-comparison.md`). Nothing here
was measured against a live broker or a live market session, and no security scan, load test or
penetration test was run. Where a claim rests on a vendor's documentation, it says so.

## 0. Progress and corrections (updated 2026-10-31)

A second pass (six read-only explorers over the gaps, the backend/frontend wiring and the UI) checked every
claim below against commit `4d32fa2` and found more. The plan to close everything is a twelve-phase one;
this is where it stands.

**Closed in phase 1 (foundations and quick fixes):**

| Gap | What changed |
|---|---|
| G14 | `tests/test_migration_schema_parity.py` builds one database with `alembic upgrade head` and one with `create_all()` and compares what Postgres says about both (columns, constraint definitions, indexes, enum labels, triggers, functions); also runs `alembic check` and asserts a single head. It sees what `alembic check` cannot (CHECKs, enums, defaults, triggers) and names the exact line that differs. |
| G15 | The two stub packages (`src/execution`, `src/risk`) are deleted; the heartbeat isolation proof is now an import-closure test in a fresh interpreter, with a positive control, instead of a `hasattr` check on names that no longer need to exist. |
| G16 | Fixed: the local-date tests (`date.today()` against UTC bounds), the fixed 2030 option expiry, and the real-clock lag test (`tests/clock.py` is the shared frozen clock). Still open, as a watch item: four tests that read IST "today" twice and could flake only if a run straddles 00:00 IST. |
| G25 | Post-trade review, investor reports, the portfolio's "today" P&L, the trading-holiday gauge and the price-history check now cut the day at IST, not UTC. |
| G3 (code) | Fixed-date holidays are generated to 2040 (they stopped at 2030), Ambedkar Jayanti (14 Apr) is a fixed holiday (2025 was missing it), calendar entries can carry names, and `GET /market-data/holidays?year=` lists a year and warns when the file has nothing for it. The 2026 movable dates are in `config/nse_holidays.json` (eleven weekday closures; taken from broker pages because NSE's own circular could not be opened, so re-check them). **Still open: 2027**, which NSE publishes in December. |
| G19 | New: the data lake, its backups and the audit archive are on named volumes (they were inside the container and deleted by every `down && up --build`). Upgrading needs the copy-out steps in the README. |
| G18 | New: saving a notification channel kept nothing it was not sent, so the alert-level toggle emptied the channel's token and chat id. A field left out of a save is now kept; `PATCH /notification-channels/{ch}` changes only non-secret settings; unknown alert levels are rejected; the Settings toggle uses the PATCH. |
| (reset reason) | A kill-switch reset's reason was sent and discarded. It is stored (`reset_reason`), returned by `GET /kill-switch/{mode}` and written to the audit trail with what was reset. |
| (suggestions) | `POST /strategies/{id}/suggestions` is Administrator and Portfolio Manager only, like its review and regenerate steps (it was open to every role, the read-only auditor included). |

**Closed in phase 2 (the console tells the truth, and cannot drift from the backend):**

| Item | What changed |
|---|---|
| W1, W3, W4, W6, W7, W9 | Chat Export downloads with the token; Compare shows "n/a" for a null correlation; Market Analysis and the other pages show the server's reason; the Mission Control feed shows the newest events; the Orders filter uses the server's vocabulary; static labels are gone or computed (realized P&L is labelled before costs). |
| W5 | Orders is a "Pre-trade check (dry run)" that starts empty and sends nothing; "Simulate a tick" is confirmed with the symbol when autonomy is on. |
| W8 | A banner on every page: tripped switch, reason, drawdown against its limit, since when, and a reset that stores a reason and asks for a typed confirmation; an unreadable switch is "unknown", never "armed". |
| W2 (UI) | Settings: pause or resume a channel's alerts, allowed sender IDs, the signing secret. (The wipe itself was G18.) |
| W11-W13 | A section that cannot load says so; Provider Status shows a refused token; Backtests filters by all of a strategy's versions and shows `refusal_reason`; the client types carry the fields the server sends. |
| W14, W15 | Identity fields can be cleared; a prompt is read in full before activation; every page reports a failed read (Orders, Audit, Portfolio, Strategies, Agent Fleet, Backtests, Mission Control, Analysis, Investor Reports, Chat) and polls through one hook. |
| W16, W20 (part) | Sign-in messages (slice 2); a 429 now exposes `Retry-After` to the browser. **Not done:** pagination and the summary variant of the heavy list endpoints (Phase 9). |
| W17, W21, W22 | Dead buttons removed or wired; the pages offered follow the role; tab lists, focus traps, keyboard-reachable cards and reorder buttons; Vercel Analytics and the `v0.app` tag removed; preferences read defensively; the orb honours the OS reduced-motion setting. **Not done:** the sidebar items are buttons, not links. |
| W23 | The console shell is mounted once (a route group) and the market and kill-switch reads are shared. |
| Tooling | `ignoreBuildErrors` is off (`next build` type-checks); ESLint ratchet, `pnpm typecheck` and the logic specs in the frontend job; seven new page specs and an auditor fixture. |
| Wiring guard | `tests/test_frontend_wiring.py` (every client call has a route and every route a call or a reason) and generated permissions (`lib/permissions.generated.json`, `can()`). |

**Closed in phase 3 (equity history and the automatic kill switch):**

| Gap | What changed |
|---|---|
| G1 | The kill switch trips by itself: `equity_snapshots`, a monitor that reads each mode's equity every minute while the market is open and at 15:45 IST, trips the switch at the applied limit (audit row `kill_switch.tripped` by `system:drawdown-monitor`, alert, row lock), never clears it, and re-bases its peak at a human reset. `POST /kill-switch/{mode}/evaluate` measures and judges now. |
| G11 | Equity history exists (`GET /equity/{mode}`) and is charted on the Paper Desk and Overview, with unmeasurable readings drawn as gaps and explained. |
| G23 | The monitor and the drill's default read the applied `max_drawdown_pct` (`get_effective_risk_limit` now has callers); a supplied threshold is used for that check and never stored. A catalog bounds what can be staged, and staged changes can be rejected or cancelled. |
| G22 (ledger part) | The live position moves only on a broker-confirmed fill, at the broker's price, with estimated costs; a rejected or cancelled order moves nothing; one row per strategy and symbol. **Still open (Phase 6):** the live adapter is resolved once at boot, live intents have no staleness guard, and the live circuit-band check compares a price with itself. |
| W10 | The risk-limit screen offers real limits and their range, shows every change with who and when, and can reject. |

**Closed in phase 4 (Go-Live evidence from the records):**

| Gap | What changed |
|---|---|
| G2 | The gate is judged on evidence derived from the paper fills (real-price closing fills only; generated, typed and administrative ones set aside and counted), a real-data backtest and the Shadow Mode record, shown with where each number came from and what could not be derived. Typed numbers are an override that needs a reason and a typed confirmation, and the audit trail keeps them beside the derived ones. |
| G24 | A clean Shadow Mode day is defined (a dry run for the strategy that worked and none that failed, on an NSE trading day) and derivable: runs carry their strategy and outcome, a failed run is stored, and a daily job makes the check. |
| (approvals) | Sign-off cards name the strategy and the asker, a decision can carry a reason, and decided requests stay in a history. |

**Still open from this phase:** until backtests can be run from the data lake (Phase 9) a backtest on real data does not exist, so the win-rate condition can only be met through the override.

**Closed in phase 5 (alerts):**

| Gap | What changed |
|---|---|
| G4 | A fifth alert level, `engine`, and a watcher that tells the subscribed channels once when a position is left unwatched, a feed stops, a position has no price or a price goes stale, and once when it clears; events for a stop-loss exit, a roll or square-off that could not be done, a position closed after its expiry and a broker refusing its credentials; every alert is kept as an episode and listed on the Paper Desk and counted in the top bar. Prometheus's rules reach the same channels through an Alertmanager service that posts to a token-protected webhook, and a rule watches the watcher itself. |
| (attention rules) | The Paper Desk's "needs attention" logic now exists on the server too, and the two are pinned to one shared set of cases. The stale-price limit is one setting instead of two constants. |

**Still open from this phase:** an alert about the backend being down cannot be delivered by the backend (it is visible in Prometheus and Grafana).

**Closed in phase 6 (exchange timestamps, circuit limits, the live path):**

| Gap | What changed |
|---|---|
| G3b | A quote whose exchange stamp is from an earlier IST date (or, if configured, older than a limit) is refused where every consumer reads from, counted, remembered for the desk, and not handed to another broker; no stamp or a future stamp is let through. |
| G9 | The order price is checked against the exchange's own circuit limits from the quote; the reference table is only a labelled fallback. The position limit is called what it is, a house rule. A stop-loss exit is not refused for a gap beyond the band (it used to be: the band was measured from the position's own cost). |
| G22 (rest) | The live re-quote is checked for a stale stamp; the live circuit band is judged against the exchange limits or the previous close, not the quote's own price; the live adapter is looked up on every firing; ticks older than the limit are not acted on; an order that could not be sent is an engine alert. |

**Still open from this phase:** Upstox's exchange stamp, and the circuit-limit fields of a recorded live response, need a first real session (operator); the age limit stays off until that session's lag panel says what is normal.

**Closed in phase 7 (Postgres backup and key custody):**

| Gap | What changed |
|---|---|
| G5 | A nightly `pg_dump` (22:45 IST) with a manifest that records the schema revision, the audit log's tip and every table's row count from the same snapshot as the dump; checksummed and listed by `pg_restore` before it is kept; files readable by their owner only; retention by count and age that never removes the newest complete backup; a failed night opens a critical engine alert, and a night that never ran is caught by a Prometheus rule through Alertmanager. A restore tool that verifies first, restores, and checks the result against the manifest (row counts, audit chain, audit tip, append-only trigger). A runbook with what is and is not in a backup and what each loss costs, including the key custody of `SECRETS_ENCRYPTION_KEY`, and a restore drill that was run (4 s, every check passing, also as a non-superuser). Settings > System and backups shows both kinds of backup and runs one on request. |

**Still open from this phase:** a drill on the live database's real data (the live stack has no backups until it is rebuilt); no WAL archiving, so up to a day of changes can be lost; the backups sit on the same machine as the database (copy the newest dump elsewhere); Qdrant, Redis and the monitoring volumes have no backup; `SECRETS_ENCRYPTION_KEY` custody is the operator's.

**Closed in phase 8 (access control and browser security):**

| Gap | What changed |
|---|---|
| G6 | Registration is for the first account only (an advisory lock keeps two first-run requests from both becoming administrator) and is refused in the same words whatever the email; accounts are made by an administrator in Settings > Users. Wrong passwords are counted against the account: 5 in 15 minutes lock it for 15 (423 with `Retry-After`, an audit entry, an administrator can unlock). |
| G7 | The access token is held in memory only and the refresh token is an HttpOnly, `SameSite=Strict` cookie scoped to `/api/v1/auth`, never in a body or in storage (Origin-checked, rotated, reuse audited; a retired token that returns within 30 seconds while its replacement was never used, a reply a reload cancelled, gets a fresh token instead of ending the session); a WebSocket opens with a one-time 30-second ticket instead of the token in the URL; the API sends nosniff, no-framing, no-referrer, a deny-all CSP and, when asked, HSTS; the console sends a per-response nonce CSP (`script-src 'self' 'nonce-...' 'strict-dynamic'`) and the plain headers; the API and the all-in-one image's frontend run as an unprivileged user and the frontend image as `node`. |
| G20 | `X-Forwarded-For` is believed only from `TRUSTED_PROXIES`, read from the right; an unknown email pays for a decoy argon2 check; argon2 runs on the thread pool; there is a per-address ceiling on sign-in attempts. |
| G21 | `/docs`, `/redoc` and `/openapi.json` exist only with `API_DOCS_ENABLED`. |
| G29 | Settings > Users: create, change role, deactivate and reactivate (typed confirm), sign out everywhere, unlock; the six admin routes are off the wiring allowlist. |

**Still open from this phase:** TLS and HSTS (put a proxy in front before exposing the stack, then set `TRUSTED_PROXIES`/`HSTS_ENABLED`); an XSS can still use the session while its page is open; styles keep `'unsafe-inline'`; anyone who knows an email can lock that account for 15 minutes (an administrator can unlock); the live stack has not been rebuilt, and rebuilding signs everyone out once.

**Closed in phase 9 (page completion), so far:**

| Gap | What changed |
|---|---|
| W12 (Audit), and the two Audit routes with no screen | The log is paged and filtered by the server (actor, entity type, action, entity id, text, an India-time range) with the match count in `X-Total-Count`, so every entry can be reached, not the newest 200; an entity id opens its whole trace and an actor opens a summary; both hashes are shown whole; a verification lists its three checks; exports follow the filters. |
| W7 (Orders), and the three live-trading routes with no screen | The ledger is one server-side query, filtered by mode, status, strategy and symbol, paged to the last row with the match count in `X-Total-Count`; each price says whether it is real or simulated; a new Live by strategy tab shows a strategy's positions and its live orders with the broker's id or the reason one failed, and each order's trades; the header counts come from the server; positions say "before costs" and a position nothing is watching says so. |
| Paper Desk, and the two paper-trading routes with no screen | A subscription waiting for price history offers to fetch it (with what it replaces, and the broker's reason if it refuses); a Signals tab lists the daily signals and whether each was acted on; every order is reachable (eight at a time) and the fills export as CSV; a manual close is marked; and one unreadable figure no longer blanks the desk. |
| Backtests, and the three routes with no screen | A backtest is started from a strategy, a share and two dates on the data lake's own prices; a walk-forward and a search for the best average length run on the same prices; Monte Carlo has a path count and shows the worst path; the trade ledger shows every trade with its dates, costs and net result, paged; each run says what its prices rest on and how it was made; a refused run says why. |
| Strategies | A strategy has its own address and tab links; every version is listed with its checks, sandbox run and origin, any two can be compared line by line, and the suggestions made are listed with what became of each; a live subscription's limits can be tightened (never loosened) and the subscription switched off (never on) by the administrator or risk manager, with a reason, audited, and refused while it holds shares; the board lists every live subscription. |
| Market Analysis, and the holiday route with no screen | The indicator periods can be chosen (each bounded, refused in its own words, and shown in the chart titles from what the server computed with); a period longer than the history is named; a Holiday Calendar tab lists the year with weekend holidays marked and warns when the calendar file does not cover it; the data lake page says how many trading days behind the newest daily data is and shows a failed run's error; each LLM provider shows when it last answered or failed; the view is in the address; times are India's. |
| Investor Reports | A report is read as a document and saved as a Markdown file; the form opens on the period the scheduler would report and the server refuses a period that is backwards, in the future or too long for its cadence; a hand-made report says who made it; a history that cannot be loaded is said to every role. |
| Portfolio | Each of the three reads fails on its own; the net P&L is shown beside the gross and the costs; accepting or rejecting a recommendation is confirmed first. |
| Agent Fleet and the gateway config (W18, and G28's audit actor and narration) | Every edit of the config names the version it was based on and is refused (409, nothing written) if that has moved on; one agent's settings and the LLM order are changed in place on the server; a refused edit cannot leave a broken file; a save leaves one version, not two; skills are chosen from the six the app ships (with the placeholders marked) instead of typed; an agent's narration says in sentences what it is doing and what is missing; the audit trail names the person, not `cli`. |
| Chat | A chat is renamed in place, pinned to a provider (marking the ones with no key), deleted only after a confirmation that names it, and exported as Markdown or JSON; a list that cannot be loaded says so; the conversation is announced to a screen reader; the server refuses a blank title and a provider it does not know. |
| Settings and scheduled jobs (W19) | A schedule chosen in Mission Control is kept across restarts and can be restored to the code's own; the open Settings section is in the address; disconnecting a channel and removing a broker's credentials ask first. |

**Corrections to what this document first said:**
- Fixed-date holidays were generated for 2020-2030 only (not "any year"), and the calendar file is re-read on every call.
- The paper desk's "needs attention" logic exists only in the browser (`lib/paper-trading.ts`); there is no server-side version.
- `ALERT_LEVELS` is in `lib/api.ts`, not `app/settings/page.tsx`; and `alert_levels` was never validated anywhere.
- The sentence-transformers embedding provider is built (commit `457ab83`) as an optional extra that neither Dockerfile installs.
- The roster is 30 agents, not 24.
- `reconcile_pending_trades` exists (every 15 s), so "live trades never reach `filled`" is out of date; what is still missing is that it never corrects the position of a rejected or cancelled order.
- The heartbeat stubs existed so the isolation proof had something to refer to; the proof is now stronger without them.

**Found since (new gaps, not yet fixed):**
- **G20** the rate limiter trusts a client-supplied `X-Forwarded-For`; an unknown e-mail returns faster than a wrong password; argon2 hashing blocks the event loop.
- **G21** `/docs`, `/redoc` and `/openapi.json` are unauthenticated.
- **G22** live path: `LivePosition` keeps the phantom quantity of rejected/cancelled orders; the live adapter is chosen once at boot; live intents have no staleness guard; the live circuit-band check compares a price with itself; one position row per strategy, not per symbol.
- **G23** the kill-switch threshold ignores the dual-control limit (`get_effective_risk_limit` has no caller), `POST /check` can overwrite it, and an automatic trip would write no audit row.
- **G24** the "clean shadow-mode streak" the Go-Live gate asks for is undefined and cannot be derived (`shadow_mode_runs` has no strategy or outcome).
- **G27** `prom/prometheus`, `grafana/grafana` and `temporalio/temporal` are `:latest`; Python has no lock or constraints file.
- **G28** fundamentals, valuation and macro agents have no data source. (Per-agent narration and the `"cli"` audit actor were closed in phase 9, slice 7a.)
- **G29** `/admin/users*` has no UI.

**Backend/frontend wiring audit (2026-10-30, static read of 182 endpoints and 153 client exports).** No frontend call lacks a backend
route (138 of 138 method+path pairs, query parameters and WebSocket paths match). 46 backend endpoints have no frontend caller: 26 by
design (webhooks, OAuth callbacks, health/metrics, scheduler triggers that Mission Control's "Run now" covers, redundant single-item
GETs) and 20 that are user-facing capabilities with no UI (admin users; backtest create / walk-forward / optimize; the kill-switch
check and its trip details; shadow mode; paper `backfill-history` and `signals`; live position, orders and trades per strategy;
audit trade-trace and actor-summary; approvals history). Defects, most serious first:
- **W1** Chat Export is a plain `<a href>` to a Bearer-only route: 401 for every user.
- **W2** Alert toggles wiped the channel's secrets (fixed, G18); the inbound allowlist and signing secrets have no UI at all.
- **W3** Backtests Compare crashes on a `null` correlation (`toFixed`).
- **W4** Market Analysis opens on a spinner that never resolves (the pulse endpoint answers 503 by design, no catch); five more pages and tabs do the same on a failed load.
- **W5** Manual order entry persists nothing, claims live exposure, its typed-symbol confirm can be bypassed, and its portfolio value defaults to 1,000,000.
- **W6** Mission Control's feed shows the six oldest of the latest 30 events.
- **W7** The Orders status filter does not use the backend's vocabulary (`pending_confirmation`, `failed`).
- **W8** The kill-switch trip reason, drawdown and threshold are fetched and never shown; "OFF · ARMED" is shown when both fetches failed.
- **W9** Static labels: "PAPER ENVIRONMENT" while live autonomy may be on, "verified" on every activity row, "SYSTEM ONLINE", "24 agents", "Monday 07:00".
- **W10** Risk-limit dual control has no effect on enforcement (G23) and its UI has free-text names, no history and no reject.
- **W11-W23** fabricated "not configured" cards on a failed fetch; Backtests filter and `refusal_reason`; Audit searches only the newest 200 entries; TS types that drop backend fields; "Test connection" shown to roles the backend rejects; Strategies live panel vanishing on a failed fetch; login/register mapping every non-401 to "connectivity"; palette missing three pages and a `?view=queue` link that does nothing; gateway-config read-modify-write that flattens the file and can race; scheduled-job edits that are in memory only; polling load against a 300/min limit and unbounded N+1 list endpoints; accessibility gaps; a Vercel analytics beacon on a self-hosted console; `ShellLayout` mounted inside every page.

## 1. Where the app stands

| | |
|---|---|
| Backend | 31 route files, 182 endpoints, 51 models, 28 migrations, 8 schedulers, ~39,500 lines in `backend/src` |
| Tests | 176 backend test files; the last full run: 2,323 passed, 1 skipped. 14 Playwright specs (the paper-desk logic spec alone has 38 cases) |
| Frontend | 13 pages (Overview, Mission Control, Chat, Agent Fleet, Strategies, Backtests, Orders & Trades, Paper Desk, Portfolio, Market Analysis, Investor Reports, Audit Log, Settings), all wired to the real backend |
| CI | secrets scan, ruff, bandit, `mypy --strict`, pip-audit, pytest with coverage floors, mutation testing of the safety layer, Playwright E2E, tsc, image builds and GHCR publish |
| Plans | Phases 0–26 of the build (Phase 17, the real-world pass, is still open); the real-data paper-trading plan, phases 0–7, and the desk follow-up |

What is solid: the audit trail (hash-chained, trigger-enforced), RBAC on every route, the sandbox
around strategy code, the paper engine on real quotes and real depth with the Indian cost model,
the subscription controls, the hardening of CI, and the habit of refusing to invent data.

## 2. Gaps, most important first

### P0 — before any real money is at stake

**G1. The max-drawdown kill switch never trips by itself.**
`check_drawdown` (`backend/src/orchestration/kill_switch.py:63`) is called from exactly one place:
`POST /kill-switch/{mode}/check` (`backend/src/api/routes/kill_switch.py:53`), which takes
`current_equity` and `peak_equity` **from the request body**. No scheduler or risk-gate path
computes equity from fills and positions, and no model stores an equity history. The gate
(`risk_gate.py`) reads the switch's state, but nothing sets it. Rule 1 in `CLAUDE.md` says the
deterministic safety layer is the only thing that can stop an autonomous live order; for the
drawdown rule that is currently a person calling an endpoint. The E2E spec trips it the same way.
*Fix:* persist equity snapshots (see G11), evaluate drawdown on a schedule in market hours and
after every fill, trip the paper and live switches from it, and notify (G4). *Effort: M.*

**G2. The Go-Live readiness evidence is typed by the approver.**
`POST /strategies/{id}/approve-live-eligibility` (`backend/src/api/routes/strategies.py:235`)
passes `num_trades`, `calendar_days_running`, `clean_shadow_mode_streak_days`, `live_win_rate` and
`backtest_win_rate` straight from the request body into the gate. The thresholds are fixed (30
trades, 21 days, 10 clean days, 20 pp) but the numbers measured against them are self-reported. The
paper engine now records real fills with their data source, so the system can compute these itself,
counting only fills whose data was `real`. *Fix:* derive the inputs from `paper_fills` and the
shadow-mode record; keep the body only for an explicit, audited override. *Effort: M.*

**G3. Trading-holiday calendar is out of date, and nothing checks the exchange's own timestamp.**
`config/nse_holidays.json` holds nine holidays, all 2025. Fixed-date holidays are generated for any
year, but the movable ones for 2026 (the Diwali period and Guru Nanak Jayanti among them) are
absent, so `is_market_open_ist` (`backend/src/engine/paper_trading/market_hours.py:28`) reports an
open market on those days. The tick age gate measures time since *we fetched* a quote, so a closed
market's last price, fetched just now, passes it. The exchange's own stamp is available from Kite
(`timestamp`, read into `BrokerQuote.exchange_time`) but is only measured, never used. *Fix:*
supply the 2026 (and 2027) dates from NSE's circular; refuse to act on a quote whose exchange
stamp is older than a limit; do the same for Upstox once its field is settled. *Effort: S (data) +
S (guard).*

**G4. Nobody is told.**
Prometheus rules are evaluated but there is no Alertmanager, so none of them (including
`PaperTicksStale`) reaches anyone (`observability/prometheus/prometheus.yml:26`). In the app, the
notification dispatcher has four fixed levels, `kill-switch`, `sign-off`, `go-live` and `daily`,
and the paper engine raises none of them: a broker rejecting its credentials mid-session, a
subscription halting, a stop-loss firing, a position left unmanaged, a futures roll or an expiry
square-off that failed all show only on the desk and in logs. The plan's acceptance line "halted,
alert sent" is half met. *Fix:* a paper-engine alert level (or reuse `kill-switch`/`daily`
deliberately), a small "needs attention changed" job that sends each new item once, and an
Alertmanager route for the Prometheus rules. *Effort: M.*

### P1 — operations and security

**G5. No backup of the system of record.** The data lake has a nightly checksummed backup; Postgres
(users, the audit log, subscriptions, positions, fills, approvals) has none: no `pg_dump`, no WAL
archive, no restore procedure, no test of one. The encrypted-secrets key (`SECRETS_ENCRYPTION_KEY`)
and the JWT signing key have no documented custody either; losing the first loses every stored
broker and LLM credential. *Fix:* a scheduled dump to a mounted path with retention, a restore
runbook that has been run once, and a short key-custody note. *Effort: S–M.*

**G6. Registration is open, and the first registrant becomes administrator.**
`POST /auth/register` (`backend/src/api/routes/auth.py:41`) has no off switch. On a fresh
deployment the first person to register is a SystemAdministrator; after that, anyone who can reach
the API can create a ReadOnlyAuditor account, which can read positions, orders and the audit log.
`DEFAULT_ADMIN_EMAIL`/`PASSWORD` avoids the first race but not the open door. *Fix:* a
`REGISTRATION_ENABLED` setting, off once an administrator exists, and invitation by an
administrator. Add a per-account failed-login limit; today only the blanket 300/min per IP applies.
*Effort: S.*

**G7. Browser-side hardening is absent.** No Content-Security-Policy, `X-Frame-Options`,
`X-Content-Type-Options`, `Referrer-Policy` or HSTS anywhere (API, Next.js, nginx). Access and
refresh tokens live in `localStorage` (`lib/api.ts`), so any script injection is a session theft.
WebSocket tokens travel in the URL query (redacted in the app's own logs, not in a proxy's). The
containers run as root (no `USER` in `Dockerfile`, `backend/Dockerfile`, `Dockerfile.allinone`).
*Fix:* security headers in `next.config.mjs` and the API middleware, a strict CSP, tokens in an
`HttpOnly` cookie or at least a short-lived in-memory access token, non-root users. *Effort: M.*

**G8. The paper-trading tunables could not be changed in Docker.** `docker-compose.yml` forwarded
only `PAPER_TRADING_SYNTHETIC`; the other eleven `PAPER_*` settings (tick and depth age limits,
futures margin and roll, options expiry rules, product, cost overrides) never reached the
container, so "tune `PAPER_TICK_MAX_AGE_SECONDS` from the panel" was not possible. **Closed in this
pass:** all are forwarded with the code's own defaults and documented in `.env.example`.

**G9. The regulatory data is a placeholder.** `ReferenceTableRegulatoryDataProvider`
(`backend/src/engine/risk/compliance.py:36`) gives every symbol a 10% position limit and a 20%
circuit band unless it is NIFTY or BANKNIFTY. Real circuit bands are per stock and change; Kite's
quote carries `lower_circuit_limit` and `upper_circuit_limit` (checked against its documentation),
and the position limits come from exchange circulars. This was item 25 of the Phase 20 backlog and
is still open. *Fix:* take the band from the live quote, keep the table only as a fallback that
says it is one. *Effort: S–M.*

**G10. The live path has never met a real broker or real money.** Phase 17 (the real-world pass) is
still unchecked in `CLAUDE.md`: its broker/LLM/notification setup and live-market smoke test need
the operator at a keyboard during a session. Paper trading on real quotes (phases 3–7) has not run
in a session either, as far as the repository records. Until it does, no number in the Phase 3–7
documentation has been measured. *Fix:* run the acceptance session in the real-data plan, then
record the tick-age and quote-call numbers. *Effort: operator.*

### P2 — completeness and quality

**G11. No equity history.** There is no model for equity, NAV or daily P&L snapshots. The desk
shows today's realized result and the current unrealized figure; nothing can answer "what is this
subscription's drawdown, Sharpe or equity curve", and G1 and G2 cannot be built without it.
*Fix:* an `equity_snapshots` table written at each fill and at the close, per subscription and in
total. *Effort: M.*

**G12. The frontend has a thin safety net.** No ESLint or Prettier, no unit-test runner,
`typescript.ignoreBuildErrors: true` in `next.config.mjs` (CI's `tsc` job is what catches type
errors). Seven of the thirteen pages have no E2E spec at all: Agent Fleet, Market Analysis, Audit
Log, Backtests, Chat, Investor Reports, Portfolio. *Effort: M, spread out.*

**G13. Supply-chain coverage is partial.** Dependabot watches only GitHub Actions
(`.github/dependabot.yml`), not Python, pnpm or Docker bases. There is no `pnpm audit`, image scan
or SBOM; only the backend has `pip-audit`. *Effort: S.*

**G14. No migration-drift check.** CI runs `alembic upgrade head` for the E2E database but never
`alembic check`, so a model change without a migration is found by hand. *Effort: S.*

**G15. Dead scaffolding.** `backend/src/execution/orders.py` and `backend/src/risk/limits.py` are
Phase 0 stubs that raise `NotImplementedError("... ships in a later phase")`. Nothing imports them
(the real code lives in `engine/` and `orchestration/`); they only confuse a reader about where
order placement lives. Delete them. *Effort: S.*

**G16. Time-bomb tests.** One was found this session: a market-data test used a fixed July date and
failed once real time moved 120 days past it (CI #70). Another, earlier, built "tomorrow" in UTC
where the code judges IST. There has been no sweep for the pattern. *Fix:* grep for fixed dates
against a `datetime.now()` window, and inject a clock. *Effort: S.*

**G17. Smaller data-quality items.** Upstox's exchange timestamp is unread (its documented fields
disagree). Two brokers' adjusted prices differ by about 2% on some symbols (INFY); the cause is
unconfirmed and the system pins each symbol to one broker's history instead. Zerodha's F&O cost
schedule needs checking against the current published rates. The local `sentence-transformers`
embedding provider and per-agent narration, both from the Phase 20 backlog, were not built.

### P3 — known limits, already documented

Real margin (SPAN or a broker margin calculator) is an estimate; option exercise, assignment and
stock-F&O physical settlement are modelled as a square-off at the last quote; options are only
bought or sold as defined-risk credit spreads, with no outright selling or debit spreads (the
naked-options rule accepts only the credit-spread shape); no independent corporate-actions feed;
no broker WebSocket feed (polling every 3 s; revisit once the first session's tick-age numbers
exist); the Muhurat trading session is outside the 09:15–15:30 window. Decided against, not gaps:
MFA, multi-tenancy, Vault, Kubernetes, a Casbin-style policy engine.

## 3. Items only the operator can close

- Reconnect the brokers each trading morning (their tokens end around 06:00 IST).
- The JWT signing-key rotation click in Settings.
- Run the real-data acceptance session and the Phase 17 live checks; read the results off the
  tick-age, quote-call and exchange-lag panels.
- Supply NSE's 2026 and 2027 holiday dates for `config/nse_holidays.json`.
- Keep the browser pane signed in to GitHub if CI is to be read from there: the Actions page was readable for some of the session and returned "Page not found" at other times, which is what a private repository does to a signed-out visitor.

## 4. Suggested order

1. **G11 then G1** (equity snapshots, then automatic drawdown): the safety rule that is currently
   manual, and the data that G2 and the desk's performance views also need.
2. **G4** (alerts), because an autonomous system whose problems only show on a page nobody is
   looking at is not autonomous in any useful sense; it also makes G1 worth having.
3. **G3** (calendar and exchange-timestamp guard): small, and it removes a way to trade on a closed
   market.
4. **G2** (evidence from fills), **G6** (registration), **G5** (Postgres backup).
5. **G7**, **G9**, **G12–G16** as a hardening pass; **G10** whenever a session can be run.

## 5. Documentation updated alongside this analysis

`README.md` (rewritten: it was still the v0 template), `docs/CLAUDE.md` (a current-state summary
and the open-work list at the top of the build status; corrected statements), `.env.example` and
`backend/.env.example` (the paper-trading tunables) and `docker-compose.yml` (forwarding them).
