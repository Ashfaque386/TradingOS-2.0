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
| G3 (code) | Fixed-date holidays are generated to 2040 (they stopped at 2030), Ambedkar Jayanti (14 Apr) is a fixed holiday (2025 was missing it), calendar entries can carry names, and `GET /market-data/holidays?year=` lists a year and warns when the file has nothing for it. **Still open: the 2026-27 movable dates in `config/nse_holidays.json`**, which wait for the operator to confirm them. |
| G19 | New: the data lake, its backups and the audit archive are on named volumes (they were inside the container and deleted by every `down && up --build`). Upgrading needs the copy-out steps in the README. |
| G18 | New: saving a notification channel kept nothing it was not sent, so the alert-level toggle emptied the channel's token and chat id. A field left out of a save is now kept; `PATCH /notification-channels/{ch}` changes only non-secret settings; unknown alert levels are rejected; the Settings toggle uses the PATCH. |
| (reset reason) | A kill-switch reset's reason was sent and discarded. It is stored (`reset_reason`), returned by `GET /kill-switch/{mode}` and written to the audit trail with what was reset. |
| (suggestions) | `POST /strategies/{id}/suggestions` is Administrator and Portfolio Manager only, like its review and regenerate steps (it was open to every role, the read-only auditor included). |

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
- **G28** per-agent narration is unbuilt; fundamentals, valuation and macro agents have no data source; agent edits are audited as `"cli"`.
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
