import { expect, test } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// What the trading engine has needed a person for: the panel on the Paper Desk, the count in the top
// bar that leads to it, and Settings' line about the path from Prometheus to a channel. The episodes
// are written by backend/scripts/seed_e2e_engine_alerts.py, as the watcher, the engine and
// Alertmanager would leave them; each run has its own symbol, and other specs may leave alerts of
// their own, so nothing here counts alerts in total.

test('the paper desk lists open and cleared alerts and shows the subscription an alert is about', async ({ page }) => {
  const { symbol, suffix } = parseSeedOutput(runSeedScript('seed_e2e_engine_alerts.py'))

  await login(page, E2E_USERS.pm)
  await page.goto('/paper-trading')
  const panel = page.getByRole('region', { name: 'Engine alerts' })
  await expect(panel).toBeVisible()

  const unwatched = panel.getByTestId('engine-alert').filter({ hasText: `${symbol} is holding a position nobody is watching` })
  await expect(unwatched).toHaveAttribute('data-state', 'open')
  await expect(unwatched).toContainText('The subscription is switched off, so its stop-loss no longer runs.')
  await expect(unwatched).toContainText('Raised by: Paper desk check')
  await expect(unwatched).toContainText(/Open for 1[23] min/)
  await expect(unwatched).toContainText('Sent to the channels')

  const prometheus = panel.getByTestId('engine-alert').filter({ hasText: `arriving too old to act on (${suffix})` })
  await expect(prometheus).toContainText('Raised by: Prometheus')
  await expect(prometheus.getByRole('button', { name: 'Show on the desk' })).toHaveCount(0) // not about one subscription

  // What has cleared stays in the panel, folded away.
  const cleared = panel.locator('details.pt-engine-cleared')
  await cleared.locator('summary').click()
  const stop = cleared.getByTestId('engine-alert').filter({ hasText: `${symbol}: stop-loss hit, position closed` })
  await expect(stop).toContainText('Raised by: Engine')
  await expect(stop).toContainText('Happened at the time shown')
  await expect(cleared.getByTestId('engine-alert').filter({ hasText: `${symbol} is not trading` })).toContainText('Cleared before it was sent')

  // An alert about a subscription takes you to it, opened.
  await unwatched.getByRole('button', { name: 'Show on the desk' }).click()
  await expect(page.getByTestId(`lane-${symbol}`)).toBeVisible()
  await expect(page.getByTestId(`lane-${symbol}`).locator('.pt-lane-body')).toBeVisible()
})

test('the top bar counts open alerts from any page and leads to the panel', async ({ page }) => {
  runSeedScript('seed_e2e_engine_alerts.py')

  await login(page, E2E_USERS.risk)
  const chip = page.getByTestId('engine-alerts-chip')
  await expect(chip).toBeVisible({ timeout: 15_000 })
  await expect(chip).toHaveAttribute('aria-label', /open engine alerts?, \d+ critical/)
  await expect(chip).toHaveAttribute('href', '/paper-trading#engine-alerts')

  await chip.click()

  await expect(page).toHaveURL(/\/paper-trading#engine-alerts$/)
  await expect(page.getByRole('region', { name: 'Engine alerts' })).toBeVisible()
})

test('an auditor can read the alerts too, and nothing on the panel changes them', async ({ page }) => {
  const { symbol } = parseSeedOutput(runSeedScript('seed_e2e_engine_alerts.py'))

  await login(page, E2E_USERS.auditor)
  await page.goto('/paper-trading')

  const panel = page.getByRole('region', { name: 'Engine alerts' })
  await expect(panel.getByTestId('engine-alert').filter({ hasText: `${symbol} is holding a position nobody is watching` })).toBeVisible()
  // Reading only: an alert closes when its problem is gone, there is no dismiss.
  await expect(panel.getByRole('button', { name: /dismiss|acknowledge|silence/i })).toHaveCount(0)
})

test('Settings says whether Prometheus alerts can reach the channels', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=notifications')

  const line = page.getByTestId('alertmanager-status')
  await expect(line).toBeVisible()
  // This test deployment runs without Alertmanager (ALERTMANAGER_URL is not set).
  await expect(line).toHaveAttribute('data-tone', 'off')
  await expect(line).toContainText('This deployment has no Alertmanager')
})
