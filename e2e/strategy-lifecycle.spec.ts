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
// A brand-new strategy has no fills, backtest on real data or dry runs, so the Go-Live evidence
// (derived by the system from those records) cannot pass; the last step signs it off with the
// explicit, reasoned override, which is also what the audit trail records. The derived path is
// covered in go-live-evidence.spec.ts.
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
  // Mission Control's sign-off queue names the strategy, so its cards are found by name.

  await page.getByRole('tab', { name: 'Lifecycle', exact: true }).click()
  await expect(page.getByText('Current stage: Backtesting')).toBeVisible()

  await page.getByRole('button', { name: /Request promotion to paper trading/i }).click()

  // Decide the promotion approval as an operator with decide rights.
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  await page.getByRole('tab', { name: /Sign-off queue/i }).click()
  const promotionCard = page.locator('.queue-card', { hasText: strategyName }).first()
  await expect(promotionCard).toBeVisible({ timeout: 15_000 })
  await promotionCard.getByRole('button', { name: /Approve/i }).click()
  const decision = page.getByRole('dialog', { name: /^Approve: / })
  await decision.getByLabel('Reason (optional)').fill('E2E: promotion looks sound')
  await decision.getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(promotionCard).not.toBeVisible({ timeout: 15_000 })

  // The decision stays on record, with who and why.
  const history = page.getByRole('region', { name: 'Sign-off history' })
  const decided = history.locator('tr', { hasText: strategyName })
  await expect(decided).toContainText('promotion to paper trading', { timeout: 15_000 })
  await expect(decided).toContainText('Approved')
  await expect(decided).toContainText('E2E: promotion looks sound')
  await expect(decided).toContainText(E2E_USERS.admin.email)

  // Back on the Strategies page, the now-approved promotion can be executed.
  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name: strategyName }).click()
  await page.getByRole('tab', { name: 'Lifecycle', exact: true }).click()
  await page.getByRole('button', { name: /Promote \(after approval\)/i }).click()
  await expect(page.getByText('Current stage: Paper Trading')).toBeVisible({ timeout: 15_000 })

  await page.getByRole('button', { name: /Request live-eligibility sign-off/i }).click()

  await page.goto('/mission-control')
  await page.getByRole('tab', { name: /Sign-off queue/i }).click()
  const liveEligibilityCard = page.locator('.queue-card', { hasText: strategyName }).first()
  await expect(liveEligibilityCard).toBeVisible({ timeout: 15_000 })
  await liveEligibilityCard.getByRole('button', { name: /Approve/i }).click()
  await page.getByRole('dialog', { name: /^Approve: / }).getByRole('button', { name: 'Approve', exact: true }).click()
  await expect(liveEligibilityCard).not.toBeVisible({ timeout: 15_000 })

  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name: strategyName }).click()
  await page.getByRole('tab', { name: 'Lifecycle', exact: true }).click()

  // The evidence is worked out from the records: this strategy has none, so the plain sign-off is off
  // and the panel says what could not be worked out.
  await expect(page.getByLabel('Go-live evidence')).toContainText('Could not be worked out from the records')
  await expect(page.getByRole('button', { name: 'Sign off as live eligible' })).toBeDisabled()

  // Go-Live Readiness Gate (Build Spec §8): all four conditions must still pass, here on numbers the
  // approver supplies and answers for, matching backend/tests/test_orchestration_live_trading.py's own
  // _PASSING_READINESS fixture values.
  await page.getByText('Override with numbers of your own').click()
  const evidence = page.getByLabel('Go-live evidence')
  await evidence.getByLabel(/^Trades$/).fill('50')
  await evidence.getByLabel(/Calendar days running/).fill('30')
  await evidence.getByLabel(/Clean Shadow Mode streak/).fill('15')
  await evidence.getByLabel(/Paper win rate/).fill('0.55')
  await evidence.getByLabel(/Backtest win rate/).fill('0.5')
  await evidence.getByRole('button', { name: /Sign off with these numbers/ }).click()

  const override = page.getByRole('dialog', { name: 'Sign off with your own numbers' })
  const confirm = override.getByRole('button', { name: 'Sign off with an override' })
  await expect(confirm).toBeDisabled() // a reason and the typed word are both needed
  await override.getByLabel('Why are the recorded numbers not enough?').fill('E2E: a new strategy with no history')
  await override.getByLabel('Type OVERRIDE to confirm').fill('override')
  await confirm.click()
  await expect(page.getByText('Current stage: Live Eligible')).toBeVisible({ timeout: 15_000 })
})
