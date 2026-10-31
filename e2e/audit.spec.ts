import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// The Audit Log is for the auditor and the administrator only, reads the hash-chained record, and
// lets the administrator re-verify the chain. (Any sign-in is itself an audited action, so there is
// always at least one entry by the time a spec opens the page.)

test('the read-only auditor reads the log, opens an entry and filters it', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')

  await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible()
  await expect(page.getByText(/^[1-9]\d* entries loaded$/)).toBeVisible()

  const firstToggle = page.getByRole('button', { name: /^Show details of entry/ }).first()
  await firstToggle.click()
  await expect(page.getByRole('button', { name: /^Hide details of entry/ }).first()).toHaveAttribute('aria-expanded', 'true')

  await page.getByPlaceholder('Search entity id, actor, action...').fill('no-such-entry-anywhere')
  await expect(page.getByText('No audit entries match these filters.')).toBeVisible()
})

test('the auditor cannot run the chain verification, the administrator can', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText('Chain not yet verified this session')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run verification' })).toHaveCount(0)

  await login(page, E2E_USERS.admin)
  await page.goto('/audit')
  await page.getByRole('button', { name: 'Run verification' }).click()
  await expect(page.getByText('Chain not yet verified this session')).toHaveCount(0)
})

test('the auditor can export the log as a file', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(/^[1-9]\d* entries loaded$/)).toBeVisible()

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: /Export CSV/ }).click(),
  ])

  expect(download.suggestedFilename()).toBe('audit_log_export.csv')
})

test('a portfolio manager is told the log is restricted, and the sidebar does not offer it', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await expect(page.getByRole('navigation', { name: 'Primary' }).getByRole('button', { name: 'Audit Log' })).toHaveCount(0)

  await page.goto('/audit')

  await expect(page.getByRole('heading', { name: 'Access restricted' })).toBeVisible()
  await expect(page.getByRole('table')).toHaveCount(0)
})
