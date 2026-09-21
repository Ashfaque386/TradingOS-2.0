import { test, expect } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, rejectAllPendingLiveIntents, runSeedScript } from './utils'

// Build Spec §21-22 hardening pass: the live-intent half of the Sign-off
// Queue (Build Spec §12.2) -- both paths named in the task: a human
// approving an intent before it expires, and the sweep-driven expiry of
// one nobody actioned in time. Reaching "one live order intent pending
// sign-off" requires walking the same strategy lifecycle
// e2e/strategy-lifecycle.spec.ts already covers end to end through the
// UI, so this spec seeds that prerequisite state directly via
// backend/scripts/seed_e2e_live_intent.py (mirrors
// tests/test_orchestration_live_trading.py's own proven-working
// DEMOSTOCK/seed=1/2026-09-09 fixture) and spends its own UI-driving
// budget on the part unique to this spec: the approve and expire outcomes.

test('a human can approve a live order intent before it expires', async ({ page }) => {
  await rejectAllPendingLiveIntents()
  const output = runSeedScript('seed_e2e_live_intent.py')
  const { intent_id: intentId } = parseSeedOutput(output)
  expect(intentId).toBeTruthy()

  await login(page, E2E_USERS.risk)
  await page.goto('/mission-control')
  await page.getByRole('button', { name: /Sign-off queue/i }).click()

  const intentCard = page.locator('.queue-card', { hasText: 'DEMOSTOCK' }).first()
  await expect(intentCard).toBeVisible({ timeout: 15_000 })
  await intentCard.getByRole('button', { name: /Approve/i }).click()
  await expect(intentCard).not.toBeVisible({ timeout: 15_000 })

  // The Orders & Trades page's full intent history is the durable record
  // of the sign-off outcome, independent of the (now-empty) live queue.
  // approve_live_order_intent (src/orchestration/live_trading.py) commits
  // status="approved" and then, within the same call, hands the intent to
  // _submit_intent_to_broker -- so by the time this request returns, the
  // intent has already moved past "approved" to whatever the broker
  // adapter resolved to. This suite's zerodha credentials
  // (backend/scripts/seed_e2e_users.py's sibling setup) are fixture
  // values, not a real Kite Connect session, so the realistic outcome is
  // "failed" (the broker call itself rejects), not "executed" -- proving
  // the human sign-off step is real just doesn't require a live broker
  // order to actually succeed downstream. Either way, "pending_approval"
  // must never be the resting state.
  await page.goto('/orders')
  await page.getByRole('button', { name: /Live Order Intents/i }).click()
  const historyRow = page.locator('.intent-row', { has: page.locator(`text=${intentId!.slice(0, 8)}`) })
  await expect(historyRow).toBeVisible({ timeout: 15_000 })
  await expect(historyRow.locator('.outcome')).toContainText(/EXECUTED|FAILED/i)
  await expect(historyRow.locator('.outcome')).not.toContainText(/PENDING/i)
})

test('an unactioned live order intent expires safely and is never silently lost', async ({ page }) => {
  await rejectAllPendingLiveIntents()
  const output = runSeedScript('seed_e2e_live_intent.py', ['--expire'])
  const { intent_id: intentId } = parseSeedOutput(output)
  expect(intentId).toBeTruthy()

  await login(page, E2E_USERS.risk)

  // The already-expired intent must never appear in the live sign-off
  // queue -- the queue is pending-only by construction.
  await page.goto('/mission-control')
  await page.getByRole('button', { name: /Sign-off queue/i }).click()
  await expect(
    page.locator('.queue-card', { hasText: intentId!.slice(0, 8) }),
  ).toHaveCount(0)

  // It must still be visible, truthfully labeled EXPIRED, in the full
  // history -- an expiry is a resolved outcome, not something that
  // silently vanishes from the record.
  await page.goto('/orders')
  await page.getByRole('button', { name: /Live Order Intents/i }).click()
  const historyRow = page.locator('.intent-row', { has: page.locator(`text=${intentId!.slice(0, 8)}`) })
  await expect(historyRow).toBeVisible({ timeout: 15_000 })
  await expect(historyRow.locator('.outcome')).toContainText(/EXPIRED/i)
})
