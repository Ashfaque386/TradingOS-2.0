# First session on real data

Everything the code can do without a broker has been built and tested. What is left needs you at a keyboard with
real accounts, during an NSE session. This runbook is that pass: in order, with what to look at, what a good result
is, and what to write down. It closes gap G10 and the operator items in `docs/gap-analysis-2026-10-30.md` section 3.

Nothing here is automated, and nothing here places a real order. Paper trading is the only thing that runs on
the real quotes. **Do not switch on any strategy's "Live Autonomous Trading" master switch during this pass.**
The live path stays untouched until the paper numbers below are in hand (Part 8).

## What you need

| | |
|---|---|
| A trading day | Monday to Friday, not an NSE holiday. Start about 08:30 IST, stay until after 15:30. The after-close recordings in Part 5 and Part 7 need you back at about 15:45. |
| Zerodha | a Kite Connect app (API key and secret), and the account's login. Tokens end around 06:00 IST every day, so you sign in again each morning. |
| Upstox | an API key and secret, the account's login, and the redirect URL registered on the app. |
| Optional | a hosted LLM provider key; a Telegram, Discord or Slack bot for alerts. |
| The machine | Docker running, `D:\TradingOS-2.0\TradingOS-2.0` on `main`, at least 8 GiB for the Docker VM. |

Write the findings of each part into the table at the end (**Record**) as you go. Part 9 turns it into doc updates.

## Part 0: bring the live stack up to date

First find out what is running. The backend image's build date and the commit it matches tell you:

```bash
docker inspect TradingOS-2.0-Backend --format '{{.Created}}'
docker exec TradingOS-2.0-Backend python -m alembic current
```

On 2026-11-07 the image was built on 2 November at about 21:10 IST, which is Phase 9's slice 7b, and the database
was at the repository's head revision (`f1a8c5d2e9b7`). It already has the named volumes (the data lake, its
backups and the audit archive survive a rebuild), the nightly Postgres backup, the sign-in rules and the Paper
Desk. It **does not** have what was committed after that: the WebSocket connection leak fix (`e7c3b54`), the
refresh fix for a reload (`9ba2814`), the paged lists (slice 8), the pinned and scanned build (Phase 10) and
Phase 11, which holds the cash-equity cost schedule and **`scripts/record_broker_quote.py`, which Part 5 needs**.
So rebuild before Part 5, and preferably before the session. Migrations are additive, so a rebuild is safe.

1. **Take a backup first, and keep a copy off this machine.** The stack makes one at 22:45 IST **only while the
   machine and Docker are running**: on 2026-11-07 the volume held dumps from 2 and 7 November and none for the
   nights between, because nothing was running then (the job does not catch up). Press **Back up now** in
   Settings > System and backups (or run the dump below), then copy the newest file out:

   ```bash
   docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres list
   docker cp TradingOS-2.0-Backend:/app/backups/. ./offsite-backups/
   ```

   Keep the copy outside the repository. The dump holds password hashes and the login signing keys.
2. **Keep `SECRETS_ENCRYPTION_KEY`.** It is in `.env`. If the saved broker credentials are to survive, the same
   key must be there after the rebuild. Copy it somewhere that is not this machine.
3. **Rebuild:**

   ```bash
   API_HOST_PORT=8001 node scripts/docker-up.js -d
   ```

   The volumes are kept, so nothing needs copying back. Rebuilding signs everyone out once; the first sign-in after
   is normal.

Leave the machine and Docker running overnight on a trading day if you want the 22:45 IST backup, the 07:30
history repair and the 08:00 signals to happen.

**Check:** the sign-in page loads, you can sign in with your existing account, and Settings > System and backups opens.
Do not create an account on the sign-in page: registration is closed after the first account on purpose, and a
fresh page offering "Create the first account" would mean the database is empty, which is a restore problem, not
a first-run one (see `postgres-restore.md`).

## Part 1: before the market opens (08:30 IST)

1. **Settings > Broker Config.** For each broker enter the key and secret (write-only: the page never shows them
   again), check the redirect URL shown is the one registered in the broker's developer console, then **Connect**
   and complete the broker's login. A token that is stored and not expired is what the feed uses; both brokers
   showing `never-connected` means none was saved.
2. **Settings > LLM Providers.** Ollama on the host is `http://host.docker.internal:11434`, not `localhost`. Use
   **Test** on each provider you will rely on; add a hosted provider's key if you have one and test it. Hosted
   providers have not been exercised end to end yet. Record the result.
3. **Settings > Notification Channels.** If you use a channel, save its token, add your own id under allowed
   senders, subscribe it to the levels you want, including **Engine alerts**, then send a test. Without any
   subscribed channel an alert is still listed on the Paper Desk but reaches no one.
4. **Settings > System and backups.** The newest Postgres backup and data lake backup are listed, and nothing is
   marked stale. On a fresh stack the first backup is at 22:45 IST; press **Back up now** once and confirm it
   appears with a size and checksum.
5. **Mission Control > scheduled jobs.** The jobs are listed and running. The 07:30 history repair and the 08:00
   signal job have run (or run today).
6. **Overview and the top bar.** The kill switch reads armed (not "unknown"), the feed reads connected once a
   broker is, and the market state shows the right phase for the time.
7. **The holiday calendar.** `GET /api/v1/market-data/holidays?year=2026` lists the file's dates; `2027` should
   warn that it has none. If today is a holiday NSE actually announced and the calendar does not know it, stop and
   fix the file first (Part 6).

**Good:** both brokers say connected with a token expiry later today; the feed is not "unavailable". **Bad:** a
broker `credentials rejected` or `circuit open` message. The message names the cause (a wrong key, an expired
token, a wrong redirect URL); fix that and reconnect, do not retry blindly: repeated bad logins can lock the
broker app.

## Part 2: enrol a small, real-data paper trade

1. **Paper Desk > Add subscription.** Choose a strategy's validated version, **Shares**, one liquid symbol (RELIANCE
   or INFY), a small capital (for example ₹100,000), stop-loss 3%. The preview shows whether the symbol is
   listed, the indicative price, and the quantity per entry.
2. A broker being connected makes enrolment fetch about a year of daily history at once. The lane should not say
   "Waiting for history". If it does, press **Fetch price history** on the lane (it reports the rows, the dates and
   the broker that supplied them, or the error).
3. **Optional, once the shares trade works:** one NIFTY future (lots: 1; allow about ₹4.7 lakh of capital for the
   margin estimate) and one `long_option`. These are API-checked but have never met a real quote. Keep their
   capital small and expect to see the **margin estimate** refuse an entry that does not fit: that is the
   behaviour, not a fault.

Why shares first: it exercises the quote feed, the depth walk, the cost model and the stop-loss on the simplest
instrument. If it misbehaves, futures and options will only add noise.

## Part 3: during the session (09:15 to 15:30 IST)

Watch these, in the order they can fail.

| Where | What to look for | If it is wrong |
|---|---|---|
| Backend logs (`docker logs -f TradingOS-2.0-Backend`) | `tick_source.selected` / `tick_source.serving_quotes` at the first cycle, naming the broker serving prices | no such line: no usable broker; recheck Part 1 |
| Paper Desk lane | a last price whose age stays under about 10 s; a freshness chip with the exchange stamp | an age above 120 s while open is flagged for attention; read the reason on the lane |
| Paper Desk > Needs attention | empty, or only what is true | any critical item (an open position nobody is watching) is real, act on it |
| Logs | `order_waiting_for_depth` | the order waits for a fresh book; a few at the open are normal; a stream of them means depth is not arriving |
| Logs | `paper_trading.tick_skipped_stale` | ticks older than `PAPER_TICK_MAX_AGE_SECONDS` (20 s) are dropped, **including the stop-loss's**. More than a handful means the feed is stalling; see Part 5 |
| A fill | a fill appears with costs; `net realized` = price result − costs on the ledger | a fill with zero costs, or a price far from the quote shown a few seconds before |
| Kill switch | stays armed; the drawdown shown is measured from equity snapshots | "cannot evaluate" is honest when there is no equity yet; it is not a trip |

When the first signal fires you should see: an order intent pass the risk gate, a fill on the real book (possibly
partial), costs charged, a row in Orders & Trades, and an audit entry. Note the time of the quote it filled
against and the fill price.

**Also tick off the Phase 17 checks that need a live market**, the ones the earlier pass could not do because NSE
was closed:

- Live ticks reach the desk (above).
- **Market Analysis > Options**: a live option chain with LTP, OI and IV (Zerodha's IV is a labelled estimate, not
  the broker's).
- **Overview > System Vitals** shows real LLM token use after a chat or pipeline run, and order-dispatch figures
  once anything has been routed (nothing will have been, in this pass; empty is correct, not zero).
- The **Organization Pulse** orb and the vitals panel change when the **paper** kill switch trips. To see it, use
  Settings > Risk Limits > "Drill: judge numbers you type", enter an equity well below a peak (say 80 against 100),
  run it on **paper** and type the confirmation. **This really trips the paper switch**, and it stays tripped until
  someone resets it with a reason (kill-switch banner or panel). Reset it straight afterwards and confirm the orb
  returns to normal. Do **not** run it on live.

## Part 4: the numbers (after 15:30)

Open Grafana (`http://localhost:3001`, user `admin`; change the password if you have not) > **TradingOS 2.0 -
Overview**. Set the range to today. Read off:

| Panel | Write down | What it decides |
|---|---|---|
| Paper ticks: age when drained / time to decision | p50 and p95 of both | whether `PAPER_TICK_MAX_AGE_SECONDS=20` is right. p95 near 20 s means the limit will drop real ticks; p95 around 8 s is the polling design working |
| Batched quote call duration, by broker / outcome | p95 per broker; the share of calls that are not `ok` | whether one batched call per cycle is cheap enough to poll faster |
| Exchange-stamped quote lag, by broker | p50 and p95 per broker | how stale an exchange stamp is in practice; the basis for `PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS` |
| Ticks skipped as stale / quotes with no usable exchange stamp | totals | stale skips should be near zero; "no usable stamp" is expected for Upstox until Part 5 |
| Order dispatch latency by broker | nothing yet | empty is correct without live orders |

**Decisions the numbers feed:**

- If tick age p95 is dominated by the 3 s and 5 s job cadence and not by the broker, shorten the intervals first.
  Only if the broker's own call duration is the problem does a WebSocket feed become worth building (gap-analysis
  section P3).
- **Set `PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS`.** It defaults to 0, meaning off: a quiet stock has a legitimately
  old stamp, so a guess could refuse good quotes. Choose a value comfortably above the p95 lag of a liquid
  stock (a first choice: p95 × 2, rounded up, and not below 60). Put it in `.env`, then recreate the backend so the
  container sees it (`docker-compose.yml` forwards it), with the same `API_HOST_PORT` you started the stack with
  (`API_HOST_PORT=8001 docker compose up -d backend`). A stamp from an earlier IST date is refused regardless of
  this setting.
- If `PAPER_TICK_MAX_AGE_SECONDS` should change, set it the same way.

## Part 5: the Upstox exchange stamp (two recordings)

Upstox's published quote fields disagree on which one is the exchange time, so the code reads none for Upstox and
the staleness guard fails open there. Two real answers settle it. They hold no credentials.

With Upstox connected, in the backend container:

```bash
docker exec TradingOS-2.0-Backend python -m scripts.record_broker_quote /tmp/upstox-open.json INFY RELIANCE
```

Run it **once while the market is open** (for example 11:00 IST) and **again after the close** (about 15:45 IST) as
`/tmp/upstox-close.json`. Copy them out and keep them:

```bash
docker cp TradingOS-2.0-Backend:/tmp/upstox-open.json .
docker cp TradingOS-2.0-Backend:/tmp/upstox-close.json .
```

Each file has `captured_at_utc`, `nse_session_open`, and per symbol the untouched response. What to look at: the
quote's `timestamp` and `last_trade_time` in both files. During the session one of them should be within seconds
of `captured_at_utc` (shifted to IST) and the other may be something else (an epoch in milliseconds, or a time
that does not move); after the close the real exchange stamp stops advancing near 15:30 while a fetch-time field
keeps following the clock. Whichever field behaves that way is the exchange stamp. Hand both files to a session;
the fix is `UpstoxAdapter._exchange_time` plus a test built on the recorded shape (and the existing test that
pins today's `None` changes with it).

## Part 6: calendar and dates

- **2027 holidays.** `config/nse_holidays.json` has 2025 and 2026. 2026's dates were taken from broker pages, not
  NSE's circular. Open NSE's trading-holiday circular for 2026 and compare each date; correct any difference.
  When NSE publishes 2027's list (normally around December), add its movable dates to the file; fixed-date
  holidays are generated in code. The endpoint `GET /market-data/holidays?year=2027` warns while the year is
  missing. Restart the backend after editing the file.
- **JWT signing key.** Settings > Security > **Rotate signing key** (SystemAdministrator). Everyone is signed in
  again afterwards, so do it when nobody else is. Confirm `GET /api/v1/system/jwt-signing-key` shows the new
  rotation time and that you can sign in. Rotate on a schedule you will keep (every few months), and after any
  suspected leak.

## Part 7: INFY's adjusted prices

The two brokers' daily closes for INFY differ by about 2% on most days (183 of 272 measured), while NIFTY matches
exactly and RELIANCE differs on one day. The system pins each symbol to one broker's history, so nothing breaks,
but which broker is right is unknown. To settle it:

1. Find an INFY corporate action in the last year (a bonus, split or large dividend) and its ex-date from NSE.
2. Fetch INFY daily bars across it from both brokers (Zerodha's historical data and Upstox's historical candles; the
   adapters' `get_historical_candles` do this: `docker exec -it TradingOS-2.0-Backend python` and call it for each
   adapter built from the stored credentials).
3. Compare each broker's series, before and after the ex-date, with NSE's published (unadjusted) close and a
   hand adjustment by the action's ratio. Note which one matches NSE and where the other departs.
4. Record the ex-date, the action, which broker is right, and the size of the other's error.

Until then, leave the pin as it is.

## Part 8: before anything live

The live path has never met a broker or money. This pass does **not** change that, but it collects what a later
live checklist needs:

- the quote-call and tick-age numbers from Part 4, on both brokers;
- whether the Paper Desk's fills, costs and stop-loss behaved on real depth for at least one full day;
- the kill switch, alerts and notification delivery working (a real message arrived);
- the restore drill on real data (below);
- the Go-Live evidence panel showing the strategy's real numbers (trades, days running, shadow-mode streak).

Do not enable "Live Autonomous Trading" for any strategy from this runbook. When you are ready to, that is a
separate, deliberate decision with its own checklist.

### The restore drill on real data

A backup nobody has restored is a hope. After the first nightly backup (or **Back up now**):

```bash
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres verify latest
docker exec TradingOS-2.0-Backend python -m scripts.restore_postgres restore latest --into tradingos_check --confirm tradingos_check
docker exec TradingOS-2.0-Backend psql -U tradingos -d postgres -c "DROP DATABASE tradingos_check"
```

The restore prints its checks: row counts equal the manifest's, the audit chain verifies, the append-only trigger is
present. Record the size, how long it took, and the output's last lines in the drill record of
`docs/runbooks/postgres-restore.md` (its "Not yet done" line names this step). Copy the newest dump off the machine
as that runbook says.

## Part 9: afterwards

1. **Stop anything you started for the test** that you do not want to keep trading: switch off the test
   subscriptions (close their positions first) in Paper Desk.
2. **Record** the table below, and tell a session the results. It will:
   - set `PAPER_TICK_MAX_AGE_SECONDS` and `PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS` defaults if the numbers say so;
   - read the Upstox exchange stamp from the two recordings;
   - settle the INFY pin from your comparison;
   - strike G10 and G17 in the gap analysis and update the Phase 17 table in `docs/phase17-realworld-testing.md`.
3. Next trading morning: reconnect both brokers again (Part 1, step 1). Tokens end around 06:00 IST; a desk with an
   expired token reads `credentials rejected` and the feed is down until you do.

## Record

| Item | Result |
|---|---|
| Date, and whether it was a normal session | |
| Zerodha connected, token expiry | |
| Upstox connected, token expiry | |
| LLM providers tested (which, pass or fail) | |
| Notification channel: real message received | |
| First shares fill: symbol, time, quote price, fill price, partial or full, costs | |
| Stop-loss exercised (yes/no, outcome) | |
| Futures and options entered (yes/no, what the margin estimate did) | |
| Tick age p50 / p95 (s) | |
| Time to decision p50 / p95 (s) | |
| Quote call p95 per broker (s) and share not ok | |
| Exchange lag p50 / p95 per broker (s) | |
| Stale skips; unusable stamps | |
| `PAPER_QUOTE_MAX_EXCHANGE_AGE_SECONDS` chosen | |
| Upstox recordings kept (open / close) | |
| INFY: ex-date, action, which broker matches NSE, size of the other's error | |
| 2026 holidays checked against NSE's circular (differences) | |
| JWT key rotated (time) | |
| Restore drill: duration, size, result | |
| Anything that did not behave as this document says | |
