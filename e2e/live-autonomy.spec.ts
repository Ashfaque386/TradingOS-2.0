import { test, expect } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// Build Spec §21-22 hardening pass, Phase 18 redesign: the per-order
// human-approval gate (this file used to be signoff-queue.spec.ts,
// testing a human approving/rejecting a pending live order intent from
// Mission Control) was removed entirely -- see backend
// Non-Negotiable Rule #1. This spec covers what replaced it: a strategy's
// master autonomy switch (Strategies page), driven through the real UI
// with its own typed confirmation modal, and the deterministic safety
// layer (Kill Switch, standing rate/notional caps) that is now the only
// thing able to block an order. Reaching a fresh LiveEligible strategy
// through the UI alone would mean re-testing the full strategy lifecycle
// (already covered by e2e/strategy-lifecycle.spec.ts) as a prerequisite
// for every spec here, so backend/scripts/seed_e2e_live_intent.py seeds
// that one prerequisite state directly and this spec spends its own
// UI-driving budget on the part unique to Phase 18.

const API_BASE_URL = 'http://localhost:8000'

async function adminToken(): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.admin),
  })
  const { access_token } = (await res.json()) as { access_token: string }
  return access_token
}

async function runDailySignal(token: string): Promise<void> {
  await fetch(`${API_BASE_URL}/api/v1/live-trading/daily-signal-run`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ as_of: '2026-09-09' }),
  })
}

async function listIntentsForStrategy(token: string, strategyId: string): Promise<{ status: string }[]> {
  const res = await fetch(`${API_BASE_URL}/api/v1/live-trading/intents?strategy_id=${strategyId}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  return (await res.json()) as { status: string }[]
}

async function seedLiveEligibleStrategy(): Promise<{ strategyId: string; symbol: string; name: string }> {
  const output = runSeedScript('seed_e2e_live_intent.py')
  const parsed = parseSeedOutput(output)
  expect(parsed.strategy_id).toBeTruthy()
  expect(parsed.name).toBeTruthy()
  return { strategyId: parsed.strategy_id, symbol: parsed.symbol, name: parsed.name }
}

// Each seed run gets its own uniquely-named strategy, but the shared dev
// DB this suite runs against keeps every previously-seeded "E2E Live
// Autonomy *" card around too -- a loose name regex resolves to multiple
// heading matches (Playwright strict mode) as soon as more than one test
// has run against it, so every caller here matches the exact generated
// name instead.
async function openStrategy(page: import('@playwright/test').Page, name: string) {
  await page.getByRole('heading', { level: 3, name, exact: true }).click()
}

async function enroll(page: import('@playwright/test').Page, symbol: string, maxIntentsPerWindow?: number) {
  await page.getByRole('button', { name: 'Lifecycle', exact: true }).click()
  await expect(page.getByText('Current stage: Live Eligible')).toBeVisible({ timeout: 15_000 })
  await page.getByLabel('Symbol').fill(symbol)
  if (maxIntentsPerWindow !== undefined) {
    await page.getByLabel(/Max orders \/ window/i).fill(String(maxIntentsPerWindow))
  }
  await page.getByRole('button', { name: /^Enroll in live trading$/i }).click()
  await expect(page.getByText('Autonomous trading is OFF')).toBeVisible({ timeout: 15_000 })
}

async function enableAutonomy(page: import('@playwright/test').Page, symbol: string) {
  await page.getByRole('button', { name: /^Enable autonomous trading$/i }).click()
  await page.getByPlaceholder(`Type ${symbol} to confirm`).fill(symbol)
  await page.getByRole('button', { name: /Confirm and enable autonomy/i }).click()
  await expect(page.getByText('LIVE AUTONOMOUS TRADING IS ON')).toBeVisible({ timeout: 15_000 })
}

test('a strategy with autonomy off generates no live order at all, even with a real signal', async ({
  page,
}) => {
  const { strategyId, symbol, name } = await seedLiveEligibleStrategy()
  const token = await adminToken()

  await login(page, E2E_USERS.admin)
  await page.goto('/strategies')
  await openStrategy(page, name)
  await enroll(page, symbol)
  // run_live_daily_signal_generation only updates subscriptions that
  // already exist, so this must run after enroll() creates one -- see
  // the "enabling autonomy..." test below for the full explanation.
  await runDailySignal(token)

  await page.getByPlaceholder('tick price').fill('106')
  await page.getByRole('button', { name: /^Generate intent$/i }).click()
  await expect(page.getByText('No intent yet this session.')).toBeVisible()

  const intents = await listIntentsForStrategy(token, strategyId)
  expect(intents).toEqual([])
})

test('enabling autonomy through the confirmation modal auto-resolves the next real signal, never pending', async ({
  page,
}) => {
  const { strategyId, symbol, name } = await seedLiveEligibleStrategy()
  const token = await adminToken()
  // Fixture-only broker credentials (never real) so build_configured_adapter
  // returns a real adapter -- the realistic outcome is the broker call
  // itself failing at this sandbox's own egress boundary, not a stuck
  // "generated" row. Mirrors e2e/README.md's own documented setup step.
  await fetch(`${API_BASE_URL}/api/v1/broker-credentials/zerodha`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: 'e2e-test-api-key',
      api_secret: 'e2e-test-api-secret',
      access_token: 'e2e-test-access-token',
    }),
  })

  await login(page, E2E_USERS.admin)
  await page.goto('/strategies')
  await openStrategy(page, name)
  await enroll(page, symbol)
  await enableAutonomy(page, symbol)
  // run_live_daily_signal_generation (src/orchestration/live_trading.py)
  // only recomputes last_signal_type for subscriptions that already
  // exist in the DB -- calling it before enroll() creates one means this
  // strategy's signal is never set, so generate_live_order_intent later
  // finds no BUY/SELL candidate and silently no-ops. Must run after enroll.
  await runDailySignal(token)

  await page.getByPlaceholder('tick price').fill('106')
  await page.getByRole('button', { name: /^Generate intent$/i }).click()
  await expect(page.getByText(/status\s+(SUBMITTED|FAILED)/i)).toBeVisible({ timeout: 15_000 })

  const intents = await listIntentsForStrategy(token, strategyId)
  expect(intents).toHaveLength(1)
  expect(intents[0].status).not.toBe('generated')
  expect(['submitted', 'failed']).toContain(intents[0].status)

  // Orders & Trades shows the same real, resolved outcome -- the durable
  // record of what the safety layer let through or blocked, independent
  // of this workspace's own transient "Last result" line.
  await page.goto('/orders')
  await page.getByRole('button', { name: /Live Order Intents/i }).click()
  const historyRow = page.locator('.intent-row', { hasText: symbol }).first()
  await expect(historyRow).toBeVisible({ timeout: 15_000 })
  await expect(historyRow.locator('.outcome')).not.toContainText(/GENERATED|PENDING/i)
})

test('a tripped kill switch blocks generation even with autonomy enabled', async ({ page }) => {
  const { strategyId, symbol, name } = await seedLiveEligibleStrategy()
  const token = await adminToken()

  await login(page, E2E_USERS.admin)
  await page.goto('/strategies')
  await openStrategy(page, name)
  await enroll(page, symbol)
  await enableAutonomy(page, symbol)
  // Must run after enroll() -- see the "enabling autonomy..." test above.
  await runDailySignal(token)

  const tripRes = await fetch(`${API_BASE_URL}/api/v1/kill-switch/live/check`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_equity: 50_000, peak_equity: 100_000 }),
  })
  expect(((await tripRes.json()) as { tripped: boolean }).tripped).toBe(true)

  try {
    await page.getByPlaceholder('tick price').fill('106')
    await page.getByRole('button', { name: /^Generate intent$/i }).click()
    // The panel surfaces the real backend error (KillSwitchTrippedError's
    // own message via ApiError), not a generic fallback -- assert on the
    // substance (kill switch tripped), not the exact wording.
    await expect(page.getByText(/kill switch is tripped/i)).toBeVisible({
      timeout: 15_000,
    })

    const intents = await listIntentsForStrategy(token, strategyId)
    expect(intents).toEqual([])
  } finally {
    // Shared dev DB -- never leave the live kill switch tripped for
    // whichever spec runs next.
    await fetch(`${API_BASE_URL}/api/v1/kill-switch/live/reset`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason: 'e2e cleanup' }),
    })
  }
})

test('the standing rate cap blocks a runaway signal independent of the kill switch', async ({ page }) => {
  const { strategyId, symbol, name } = await seedLiveEligibleStrategy()
  const token = await adminToken()

  await login(page, E2E_USERS.admin)
  await page.goto('/strategies')
  await openStrategy(page, name)
  await enroll(page, symbol, 1)
  await enableAutonomy(page, symbol)
  // Must run after enroll() -- see the "enabling autonomy..." test above.
  await runDailySignal(token)

  // First tick: a real attempt, consuming the cap's one slot for this window.
  await page.getByPlaceholder('tick price').fill('106')
  await page.getByRole('button', { name: /^Generate intent$/i }).click()
  await expect(page.getByText(/status\s+(GENERATED|SUBMITTED|FAILED)/i)).toBeVisible({ timeout: 15_000 })

  // Second tick, immediately after: the cap must block it before it's
  // ever attempted -- no Kill Switch involved at all here.
  await page.getByPlaceholder('tick price').fill('107')
  await page.getByRole('button', { name: /^Generate intent$/i }).click()
  await expect(page.getByText(/status\s+CAPPED/i)).toBeVisible({ timeout: 15_000 })

  const intents = await listIntentsForStrategy(token, strategyId)
  const capped = intents.filter((i) => i.status === 'capped')
  expect(capped).toHaveLength(1)
})
