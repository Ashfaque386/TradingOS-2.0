import { expect, test, type Route } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// Market Analysis: the view tabs, and the Provider Status tab saying what the broker said, not what the
// clock says. (The 503 on the Pulse tab is covered in wiring-fixes.spec.ts.)

function respond(route: Route, body: unknown, status = 200) {
  const origin = route.request().headers()['origin'] ?? '*'
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: { 'access-control-allow-origin': origin, 'access-control-allow-credentials': 'true' },
    body: JSON.stringify(body),
  })
}

test('the view tabs are a tab list: arrow keys move between them and open them', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/analysis')
  const tabs = page.getByRole('tablist', { name: 'Market analysis views' })
  await expect(tabs).toBeVisible()

  const pulse = tabs.getByRole('tab', { name: 'Pulse' })
  await expect(pulse).toHaveAttribute('aria-selected', 'true')
  await pulse.focus()
  await page.keyboard.press('End')

  await expect(tabs.getByRole('tab', { name: 'Live Option Chain' })).toHaveAttribute('aria-selected', 'true')
  await expect(pulse).toHaveAttribute('aria-selected', 'false')
})

test('the Provider Status tab lists the LLM providers and the broker connections', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/analysis')

  await page.getByRole('tab', { name: 'Provider Status' }).click()

  await expect(page.getByText('LLM providers', { exact: true })).toBeVisible()
  await expect(page.getByText('Broker connections', { exact: true })).toBeVisible()
  await expect(page.getByText(/circuit closed/).first()).toBeVisible()
})

test('a broker that refused its token is shown as refused, not as valid by the clock', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.admin)
  const real = await api<Array<Record<string, unknown>>>(token, '/api/v1/broker-credentials')
  const refused = real.body.map((broker, index) =>
    index === 0
      ? { ...broker, configured: true, token_status: 'valid', feed_rejected_at: '2026-10-30T09:20:00Z', feed_rejection_status: 401 }
      : broker,
  )
  const name = String(refused[0].broker)

  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/broker-credentials', (route) => respond(route, refused))
  await page.goto('/analysis')
  await page.getByRole('tab', { name: 'Provider Status' }).click()

  await expect(page.getByText(`${name} refused the stored token (HTTP 401), so live quotes are not flowing`)).toBeVisible()
  await expect(page.getByText('token valid (by the clock)')).toHaveCount(0)
})

test('a provider status that cannot be read says so, with a way to try again', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/broker-credentials', (route) => respond(route, { detail: 'down' }, 500))
  await page.goto('/analysis')
  await page.getByRole('tab', { name: 'Provider Status' }).click()

  const notice = page.getByRole('alert').filter({ hasText: 'Could not load the provider status' })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
})
