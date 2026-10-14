import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Settings > Broker: the token strip used to be driven by the clock alone (the daily
// "expires around 06:00 IST" estimate), so a token the broker was refusing still read
// "Connected". The backend now reports what the broker actually said; this checks the
// page believes it over the clock.
//
// A real rejection needs a live broker, which CI does not have, so the status response
// is replaced: this is about how the page renders it.

const row = (overrides: Record<string, unknown>) => ({
  broker: 'zerodha',
  configured: true,
  token_status: 'valid',
  token_expires_at: new Date(Date.now() + 6 * 3600 * 1000).toISOString(),
  redirect_uri: 'http://localhost:8000/api/v1/broker-credentials/zerodha/callback',
  default_redirect_uri: 'http://localhost:8000/api/v1/broker-credentials/zerodha/callback',
  redirect_uri_is_custom: false,
  token_duration: null,
  feed_rejection_status: null,
  feed_rejection_detail: null,
  feed_rejected_at: null,
  ...overrides,
})

async function stubStatus(page: import('@playwright/test').Page, zerodha: Record<string, unknown>) {
  await page.route('**/api/v1/broker-credentials', async (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([row(zerodha), row({ broker: 'upstox', configured: false, token_status: 'never-connected', token_expires_at: null })]),
    })
  })
}

test('a token the broker refuses is shown as rejected, not as connected', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await stubStatus(page, {
    feed_rejection_status: 403,
    feed_rejection_detail: 'Incorrect `api_key` or `access_token`.',
    feed_rejected_at: new Date().toISOString(),
  })

  await page.goto('/settings?section=broker')

  const card = page.locator('.set-broker-card').filter({ hasText: /zerodha/i }).first()
  await expect(card.getByText('Rejected by the broker — reconnect')).toBeVisible()
  await expect(card.getByText(/^Connected — expires/)).toHaveCount(0)
  await expect(card.getByRole('alert')).toContainText('refused the stored token (HTTP 403)')
  await expect(card.getByRole('alert')).toContainText('only an estimate')
})

test('a token the broker has not refused still reads as connected', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await stubStatus(page, {})

  await page.goto('/settings?section=broker')

  const card = page.locator('.set-broker-card').filter({ hasText: /zerodha/i }).first()
  await expect(card.getByText(/^Connected — expires/)).toBeVisible()
  await expect(card.getByText('Rejected by the broker — reconnect')).toHaveCount(0)
})
