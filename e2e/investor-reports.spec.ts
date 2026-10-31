import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Investor reports: a portfolio manager generates one for a period, and a read-only role can read the
// history but is not offered the form.

test('a portfolio manager generates a report and finds it in the history', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  await expect(page.getByRole('heading', { name: 'Investor reporting' })).toBeVisible()

  // A period of its own, so the entry cannot be confused with another spec's or the scheduler's.
  const start = '2019-03-04'
  const end = '2019-03-10'
  await page.getByLabel('Period start').fill(start)
  await page.getByLabel('Period end').fill(end)
  await page.getByLabel('Cadence').selectOption('weekly')
  await page.getByRole('button', { name: 'Generate now' }).click()

  const entry = page.getByRole('button', { name: new RegExp(`${start} → ${end}`) }).first()
  await expect(entry).toBeVisible({ timeout: 20_000 })
  await entry.click()
  await expect(page.getByText(/REPORT HISTORY · [1-9]/)).toBeVisible()
})

test('a read-only auditor can read reports but is not offered the form', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/investor-reports')

  await expect(page.getByRole('heading', { name: 'Investor reporting' })).toBeVisible()
  await expect(page.getByText(/REPORT HISTORY · \d+/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Generate now' })).toHaveCount(0)
})

test('a report list that cannot be loaded says so', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/investor-reports', (route) =>
    route.request().method() === 'GET'
      ? route.fulfill({
          status: 500,
          contentType: 'application/json',
          headers: { 'access-control-allow-origin': route.request().headers()['origin'] ?? '*' },
          body: JSON.stringify({ detail: 'the report store is down' }),
        })
      : route.fallback(),
  )
  await page.goto('/investor-reports')

  await expect(page.getByText('Could not load the reports: the report store is down')).toBeVisible()
})
