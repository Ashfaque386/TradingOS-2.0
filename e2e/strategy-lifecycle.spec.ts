import { test, expect } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Build Spec §21-22 hardening pass: the full strategy pipeline (Build
// Spec §9) driven entirely through the browser against the real backend
// -- Ideation -> (generate/validate/sandbox, synchronous within the
// create call) -> request-promotion -> human sign-off in Mission
// Control's queue -> Backtesting -> Paper Trading -> request-live -> a
// second human sign-off -> Live Eligible. Two operators are used
// deliberately (pm creates and requests; admin decides and promotes) so
// this also exercises the dual-control RBAC boundary between
// _OPERATOR_ROLES and the approvals endpoint's _DECIDE_ROLES, not just
// the happy path of one super-user clicking through everything.
//
// This test is also what surfaced a real, previously-shipped gap: the
// Go-Live Readiness Gate (src/engine/risk/go_live_gate.py) requires a
// non-null backtest_win_rate, both gates required, but the Strategies
// page had no input field for it at all -- meaning the live-eligibility
// sign-off could never actually be approved through the browser, no
// matter what an operator entered. Fixed alongside this spec (see
// app/strategies/page.tsx's "Backtest win rate" field).
test('strategy moves through its full lifecycle to Live Eligible', async ({ page }) => {
  const strategyName = `E2E Lifecycle ${Date.now()}`

  await login(page, E2E_USERS.pm)
  await page.goto('/strategies')

  await page.getByPlaceholder('Strategy name').fill(strategyName)
  await page.getByPlaceholder(/objective/i).fill('Buy DEMOSTOCK on a bullish SMA crossover')
  await page.getByRole('button', { name: /Generate/i }).click()

  const strategyCard = page.getByRole('heading', { level: 3, name: strategyName })
  await expect(strategyCard).toBeVisible({ timeout: 20_000 })
  await strategyCard.click()

  await expect(page.getByRole('heading', { level: 1, name: strategyName })).toBeVisible()
  // Mission Control's sign-off queue cards identify a strategy only by
  // "strategy · <8-char id>" (SignoffQueue in app/mission-control/page.tsx),
  // never by name -- capture the id shown here to find the right card later.
  const strategyIdText = await page.locator('.strategy-review-hero').getByText(/STRATEGY ID/).textContent()
  const strategyIdPrefix = strategyIdText!.split('·')[1]!.trim().toLowerCase()

  await page.getByRole('button', { name: 'Lifecycle', exact: true }).click()
  await expect(page.getByText('Current stage: Backtesting')).toBeVisible()

  await page.getByRole('button', { name: /Request promotion to paper trading/i }).click()

  // Decide the promotion approval as an operator with decide rights.
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  await page.getByRole('button', { name: /Sign-off queue/i }).click()
  const promotionCard = page.locator('.queue-card', { hasText: strategyIdPrefix }).first()
  await expect(promotionCard).toBeVisible({ timeout: 15_000 })
  await promotionCard.getByRole('button', { name: /Approve/i }).click()
  await expect(promotionCard).not.toBeVisible({ timeout: 15_000 })

  // Back on the Strategies page, the now-approved promotion can be executed.
  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name: strategyName }).click()
  await page.getByRole('button', { name: 'Lifecycle', exact: true }).click()
  await page.getByRole('button', { name: /Promote \(after approval\)/i }).click()
  await expect(page.getByText('Current stage: Paper Trading')).toBeVisible({ timeout: 15_000 })

  await page.getByRole('button', { name: /Request live-eligibility sign-off/i }).click()

  await page.goto('/mission-control')
  await page.getByRole('button', { name: /Sign-off queue/i }).click()
  const liveEligibilityCard = page.locator('.queue-card', { hasText: strategyIdPrefix }).first()
  await expect(liveEligibilityCard).toBeVisible({ timeout: 15_000 })
  await liveEligibilityCard.getByRole('button', { name: /Approve/i }).click()
  await expect(liveEligibilityCard).not.toBeVisible({ timeout: 15_000 })

  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name: strategyName }).click()
  await page.getByRole('button', { name: 'Lifecycle', exact: true }).click()

  // Go-Live Readiness Gate (Build Spec §8): all four conditions must pass,
  // matching backend/tests/test_orchestration_live_trading.py's own
  // _PASSING_READINESS fixture values.
  await page.getByLabel(/^Trades$/i).fill('50')
  await page.getByLabel(/Calendar days running/i).fill('30')
  await page.getByLabel(/Clean shadow-mode streak/i).fill('15')
  await page.getByLabel(/Live win rate/i).fill('0.55')
  await page.getByLabel(/Backtest win rate/i).fill('0.5')

  await page.getByRole('button', { name: /^Approve live-eligibility$/i }).click()
  await expect(page.getByText('Current stage: Live Eligible')).toBeVisible({ timeout: 15_000 })
})
