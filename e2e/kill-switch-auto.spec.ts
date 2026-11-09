import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, runSeedScript, tokenFor } from './utils'

// The automatic kill switch (Phase 3): the monitor measures equity, trips the switch when the fall
// from its highest point reaches the applied limit, says why on every page, and a human reset
// re-bases the peak so it does not trip again at once. Also the risk-limit screen: the limit's range
// is checked, and a staged change can be turned down by someone else with a reason.

type KillSwitch = { tripped: boolean; reset_reason: string | null }
type AuditEntry = { action: string; actor: string; entity_id: string | null }

test('the monitor trips the paper switch on a real drawdown, the banner says why, and a reset re-bases the peak', async ({ page }) => {
  const riskToken = await tokenFor(E2E_USERS.risk)
  const adminToken = await tokenFor(E2E_USERS.admin)
  // Start armed: an earlier spec's switch is put back if it was left tripped.
  const before = await api<KillSwitch>(riskToken, '/api/v1/kill-switch/paper')
  if (before.body.tripped) {
    await api(riskToken, '/api/v1/kill-switch/paper/reset', { method: 'POST', body: { reason: 'e2e: start armed' } })
  }

  runSeedScript('seed_e2e_equity.py', ['--peak', '50000000'])

  await login(page, E2E_USERS.risk)
  await page.goto('/paper-trading')
  await expect(page.getByRole('heading', { name: 'Equity and drawdown · paper' })).toBeVisible()
  await page.getByRole('button', { name: 'Measure now' }).click()

  const banner = page.getByRole('alert').filter({ hasText: 'The paper kill switch is tripped' })
  await expect(banner).toBeVisible({ timeout: 15_000 })
  await expect(banner).toContainText('reached the 15.00% limit')
  await expect(banner).toContainText('peak equity of 50,000,000.00')
  // While the market is open the backend's own minute-by-minute reading can get to the seeded fall
  // before the click does. Then the click finds the switch already tripped, and the panel says so.
  // Either way the switch tripped on the drawdown; the banner above and the audit row below prove it.
  await expect(
    page.getByText(
      /^Measured now(: the limit was reached and the kill switch tripped\.|\. The kill switch was already tripped and stays tripped until it is reset\.)$/,
    ),
  ).toBeVisible()

  // The trip is on the permanent record, attributed to the monitor and not to a person.
  const audit = await api<AuditEntry[]>(adminToken, '/api/v1/audit/entries?limit=100')
  expect(audit.body.some((e) => e.action === 'kill_switch.tripped' && e.actor === 'system:drawdown-monitor' && e.entity_id === 'paper')).toBe(true)

  // Reset it (reason and typed phrase); the same shortfall against the old peak must not trip it again.
  await banner.getByRole('button', { name: /Reset paper kill switch/i }).click()
  const dialog = page.getByRole('dialog', { name: 'Reset the paper kill switch' })
  await dialog.getByLabel('Why is it safe to resume?').fill('E2E: re-based after checking the account')
  await dialog.getByLabel('Type RESET PAPER to confirm').fill('RESET PAPER')
  await dialog.getByRole('button', { name: 'Reset paper kill switch' }).click()
  await expect(banner).toHaveCount(0, { timeout: 15_000 })

  await page.getByRole('button', { name: 'Measure now' }).click()
  await expect(page.getByText('Measured now: within the limit.')).toBeVisible({ timeout: 15_000 })
  const after = await api<KillSwitch>(riskToken, '/api/v1/kill-switch/paper')
  expect(after.body.tripped).toBe(false)
})

test('the Overview shows the equity panel for the paper account', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/')

  await expect(page.getByRole('region', { name: 'paper equity and drawdown' })).toBeVisible()
  await expect(page.getByText(/The kill switch trips when equity falls 15% below/).first()).toBeVisible()
  // The live account says why it cannot be measured, rather than showing nothing.
  await expect(page.getByRole('region', { name: 'live equity and drawdown' })).toBeVisible()
  // A portfolio manager may read the history but is not offered the measurement.
  await expect(page.getByRole('button', { name: 'Measure now' })).toHaveCount(0)
})

test('the risk limit form offers only real limits and their range, and a staged change can be rejected with a reason', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await page.goto('/settings?section=risk')

  await expect(page.getByText('Max drawdown', { exact: true }).first()).toBeVisible()
  await page.getByRole('combobox', { name: 'Limit' }).selectOption({ label: 'Max drawdown' })
  await page.getByLabel(/Proposed value/).fill('0.1')
  await expect(page.getByRole('alert').filter({ hasText: 'must be between 0.5 and 100' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Stage change' })).toBeDisabled()

  const reason = `e2e staged ${Date.now()}`
  await page.getByLabel(/Proposed value/).fill('9')
  await page.getByLabel('Reason', { exact: true }).fill(reason)
  await page.getByRole('button', { name: 'Stage change' }).click()
  const row = page.locator('.set-risk-row', { hasText: reason })
  await expect(row).toContainText('Waiting for a second person to confirm')
  await expect(row).toContainText('staged by you')

  // Someone else turns it down, with a reason; nothing is applied.
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=risk')
  const theirs = page.locator('.set-risk-row', { hasText: reason })
  await theirs.getByRole('button', { name: 'Reject' }).click()
  const dialog = page.getByRole('dialog', { name: 'Reject this change' })
  await dialog.getByLabel('Reason (optional)').fill('not now')
  await dialog.getByRole('button', { name: 'Reject the change' }).click()

  await expect(theirs).toContainText('Rejected')
  await expect(theirs).toContainText('rejected by you')
  await expect(theirs).toContainText('not now')
  const catalog = await api<Array<{ name: string; applied: boolean }>>(await tokenFor(E2E_USERS.admin), '/api/v1/risk-limits/catalog')
  expect(catalog.body.find((limit) => limit.name === 'max_drawdown_pct')?.applied).toBe(false)
})
