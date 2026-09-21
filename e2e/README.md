# End-to-end Playwright suite

Build Spec §21-22 hardening pass: a real Playwright suite driving the
actual Next.js frontend against the actual FastAPI backend, real
Postgres, and real Redis -- no mocked network responses. It covers four
flows named in that pass:

- `strategy-lifecycle.spec.ts` -- the full strategy pipeline (Ideation ->
  Backtesting -> Paper Trading -> Live Eligible), including both human
  sign-off decisions along the way.
- `signoff-queue.spec.ts` -- the live-order-intent half of the Sign-off
  Queue: a human approving an intent before it expires, and the
  sweep-driven expiry of one nobody actioned in time.
- `kill-switch.spec.ts` -- a real drawdown tripping the paper kill
  switch, and a RiskManager/SystemAdministrator resetting it from the
  Overview console (PortfolioManager sees the tripped state but no reset
  control it isn't RBAC-permitted to use).
- `agent-gateway-hot-reload.spec.ts` -- hand-editing
  `config/tradingos.config.json` with intentionally invalid JSON5 and
  confirming the app rejects it and keeps running on the last-known-good
  config, exactly as that file's own top-of-file comment promises an
  operator.

## Why not `docker compose up`

This sandbox has no reachable Docker daemon (the same constraint
`src/engine/sandbox/factory.py`'s gVisor fallback documents elsewhere), so
these tests run against services started directly on the host instead.

## Running locally

You need, running locally (not via Docker):

1. **Postgres** with a `tradingos` database and a `tradingos` role
   (`postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos`),
   migrated to head:
   ```
   cd backend && DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
     .venv/bin/python -m alembic upgrade head
   ```
2. **Redis** on the default port (`redis-server`).
3. **The backend**, on port 8000, with a broker configured so live-intent
   approval has something to submit to (fixture credentials, never real
   ones -- see `backend/scripts/seed_e2e_users.py`'s own docstring for why
   this is fixture setup, not a production credential-bootstrap path):
   ```
   cd backend
   DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
   JWT_SECRET_KEY=dev-only-secret-do-not-use-in-production \
   CORS_ORIGINS=http://localhost:3010 \
   AGENT_GATEWAY_CONFIG_PATH=../config/tradingos.config.json \
   NSE_HOLIDAYS_PATH=../config/nse_holidays.json \
   SECRETS_ENCRYPTION_KEY=<output of: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"> \
   SECRETS_STORE_PATH=secrets/e2e_broker_credentials.enc \
     .venv/bin/python -m uvicorn src.main:app --host 0.0.0.0 --port 8000
   ```
4. **The frontend**, on port 3010 (matches `playwright.config.ts`'s
   default `baseURL`; override with `E2E_BASE_URL` if you use a different
   port):
   ```
   PORT=3010 pnpm dev
   ```
5. **Fixture users**, once:
   ```
   cd backend && DATABASE_URL=postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos \
     JWT_SECRET_KEY=dev-only-secret-do-not-use-in-production \
     .venv/bin/python scripts/seed_e2e_users.py
   ```
6. **Zerodha broker credentials** (fixture values -- the live-intent
   approve spec needs *some* broker configured so `build_configured_adapter`
   doesn't return `None`; with fixture credentials the realistic outcome
   is the broker call itself failing, which is what the spec asserts):
   ```
   TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
     -H "Content-Type: application/json" \
     -d '{"email":"e2e.admin@example.com","password":"E2eAdminPass!2026"}' \
     | python3 -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
   curl -s -X POST http://localhost:8000/api/v1/broker-credentials/zerodha \
     -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{"api_key":"e2e-test-api-key","api_secret":"e2e-test-api-secret","access_token":"e2e-test-access-token"}'
   ```

Then, from the repo root:

```
npx playwright test
```

`backend/scripts/seed_e2e_live_intent.py` is called by `signoff-queue.spec.ts`
itself (via `e2e/utils.ts`'s `runSeedScript`), not something you run by
hand.
