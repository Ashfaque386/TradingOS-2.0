import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, parseSeedOutput, runSeedScript, tokenFor } from './utils'

// The Go-Live gate's inputs are worked out by the system from its own records (fills on real prices,
// a real-data backtest, Shadow Mode dry runs), not typed by the approver. Typed numbers remain an
// audited override (covered in strategy-lifecycle.spec.ts).

async function openLifecycle(page: import('@playwright/test').Page, name: string) {
  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name, exact: true }).click()
  await page.getByRole('tab', { name: 'Lifecycle', exact: true }).click()
}

test('a strategy with enough recorded evidence is signed off with nothing typed', async ({ page }) => {
  const { name, strategy_id: strategyId } = parseSeedOutput(runSeedScript('seed_e2e_go_live.py', ['passing']))

  await login(page, E2E_USERS.risk)
  await openLifecycle(page, name)

  const evidence = page.getByLabel('Go-live evidence')
  await expect(evidence).toContainText('Closing fills on real prices: 31 of 31 real-price fills.')
  await expect(evidence).toContainText('Since the first real-price fill')
  await expect(evidence).toContainText('10 of the counted days were checked against a broker')
  await expect(evidence).toContainText('Paper 51.6% against backtest 50.0% (real data)')
  for (const title of ['Trades', 'Calendar days running', 'Clean Shadow Mode streak', 'Win rate against the backtest']) {
    await expect(evidence.locator('.readiness-row', { hasText: title })).toContainText('met')
    await expect(evidence.locator('.readiness-row', { hasText: title })).not.toContainText('not met')
  }
  await expect(evidence).not.toContainText('Could not be worked out from the records')

  await evidence.getByRole('button', { name: 'Sign off as live eligible' }).click()
  const dialog = page.getByRole('dialog', { name: 'Sign off as live eligible' })
  await dialog.getByRole('button', { name: 'Sign off', exact: true }).click()
  await expect(page.getByText('Current stage: Live Eligible')).toBeVisible({ timeout: 15_000 })

  // Nothing was overridden, so no override is on the record.
  const audit = await api<Array<{ action: string; entity_id: string | null }>>(
    await tokenFor(E2E_USERS.admin),
    '/api/v1/audit/entries?limit=200',
  )
  expect(audit.body.some((e) => e.action === 'strategy.go_live_evidence_overridden' && e.entity_id === strategyId)).toBe(false)
})

test('records that cannot count are set aside and named, and the plain sign-off stays off', async ({ page }) => {
  const { name } = parseSeedOutput(runSeedScript('seed_e2e_go_live.py', ['weak']))

  await login(page, E2E_USERS.risk)
  await openLifecycle(page, name)

  const evidence = page.getByLabel('Go-live evidence')
  await expect(evidence).toContainText('Set aside: 4 on generated or typed prices, 1 hand-made cleanups.')
  await expect(evidence).toContainText('Could not be worked out from the records')
  await expect(evidence).toContainText('rest on bars this system cannot vouch for (supplied)')
  await expect(evidence).toContainText('no Shadow Mode dry run has been made for this strategy')
  await expect(evidence.getByRole('button', { name: 'Sign off as live eligible' })).toBeDisabled()
  await expect(evidence.locator('.readiness-row', { hasText: 'Trades' })).toContainText('not met')
})

test('the Shadow Mode tab shows the dry runs and the streak, and a check with no broker says why', async ({ page }) => {
  const { name } = parseSeedOutput(runSeedScript('seed_e2e_go_live.py', ['passing']))

  await login(page, E2E_USERS.risk)
  await page.goto('/strategies')
  await page.getByRole('heading', { level: 3, name, exact: true }).click()
  await page.getByRole('tab', { name: 'Shadow Mode', exact: true }).click()

  const panel = page.getByLabel('Shadow Mode', { exact: true })
  await expect(panel.getByRole('heading', { name: 'Clean streak: 10 of 10 trading days' })).toBeVisible()
  await expect(panel.locator('tbody tr')).toHaveCount(10)
  await expect(panel.getByText('Broker sandbox').first()).toBeVisible()

  // The test stack has no broker connected: the check says so rather than recording a run.
  await panel.getByLabel('Symbol').fill('E2EGL')
  await panel.getByRole('button', { name: 'Run dry check' }).click()
  await expect(panel.getByRole('alert')).toContainText(/credentials|secret/i)
  await expect(panel.locator('tbody tr')).toHaveCount(10)
})

test('an auditor reads the evidence but is not offered the sign-off', async ({ page }) => {
  const { name } = parseSeedOutput(runSeedScript('seed_e2e_go_live.py', ['passing']))

  await login(page, E2E_USERS.auditor)
  await openLifecycle(page, name)

  const evidence = page.getByLabel('Go-live evidence')
  await expect(evidence).toContainText('Closing fills on real prices: 31 of 31 real-price fills.')
  await expect(evidence).toContainText('Only an Administrator or Risk Manager can sign a strategy off.')
  await expect(evidence.getByRole('button', { name: 'Sign off as live eligible' })).toHaveCount(0)
})
