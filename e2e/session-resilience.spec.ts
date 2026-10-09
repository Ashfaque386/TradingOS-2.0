import { type Route, expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// A page load restores the session by calling /auth/me. A failure there can
// mean two very different things, and the app must tell them apart: the
// server answered "this session is over" (401, refresh refused too -- go to
// /login), or it never got to answer (rate-limited, erroring, unreachable --
// the stored tokens are still good, so the operator must NOT be signed out).
// Found when the blanket API rate limiter's 429s sent a valid session to
// /login in CI; see docs/CLAUDE.md's Phase 26 follow-up.

const TOKENS_KEY = 'tradingos.tokens'

function fulfillJson(route: Route, status: number, body: unknown) {
  const origin = route.request().headers()['origin'] ?? '*'
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'access-control-allow-origin': origin,
      'access-control-allow-headers': '*',
      'access-control-allow-methods': '*',
    },
    body: JSON.stringify(body),
  })
}

test('a session survives the session check failing with no response at all', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  // What a 429 from the rate limiter looks like to the browser: it carries no
  // CORS headers, so fetch() rejects outright instead of returning a status.
  let meCalls = 0
  await page.route('**/api/v1/auth/me', (route) => {
    meCalls += 1
    return meCalls <= 2 ? route.abort('failed') : route.continue()
  })

  await page.goto('/strategies')
  await expect.poll(() => meCalls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3)

  await expect(page).toHaveURL(/\/strategies$/)
  expect(await page.evaluate((k) => localStorage.getItem(k), TOKENS_KEY)).not.toBeNull()
})

test('a session survives the session check answering 429 or 503', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  let meCalls = 0
  await page.route('**/api/v1/auth/me', (route) => {
    meCalls += 1
    if (meCalls === 1) return fulfillJson(route, 429, { detail: 'rate limited' })
    if (meCalls === 2) return fulfillJson(route, 503, { detail: 'unavailable' })
    return route.continue()
  })

  await page.goto('/strategies')
  await expect.poll(() => meCalls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3)

  await expect(page).toHaveURL(/\/strategies$/)
  expect(await page.evaluate((k) => localStorage.getItem(k), TOKENS_KEY)).not.toBeNull()
})

test('an unreachable server shows a retry screen, then recovers without a new login', async ({
  page,
}) => {
  await login(page, E2E_USERS.admin)

  let failing = true
  await page.route('**/api/v1/auth/me', (route) =>
    failing ? route.abort('failed') : route.continue(),
  )

  await page.goto('/strategies')

  // Retries run out, so the shell stops waiting silently -- but it must not
  // redirect: the tokens are still valid.
  await expect(page.getByText("Can't reach the TradingOS server")).toBeVisible({ timeout: 20_000 })
  await expect(page).toHaveURL(/\/strategies$/)
  expect(await page.evaluate((k) => localStorage.getItem(k), TOKENS_KEY)).not.toBeNull()

  failing = false
  await page.getByRole('button', { name: 'Retry now' }).click()

  await expect(page.getByText("Can't reach the TradingOS server")).toBeHidden({ timeout: 15_000 })
  await expect(page).toHaveURL(/\/strategies$/)
})

test('a genuinely rejected session still goes to the login screen and is cleared', async ({
  page,
}) => {
  await login(page, E2E_USERS.admin)

  await page.route('**/api/v1/auth/me', (route) =>
    fulfillJson(route, 401, { detail: 'Could not validate credentials' }),
  )
  await page.route('**/api/v1/auth/refresh', (route) =>
    fulfillJson(route, 401, { detail: 'Invalid or expired refresh token' }),
  )

  await page.goto('/strategies')

  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 })
  expect(await page.evaluate((k) => localStorage.getItem(k), TOKENS_KEY)).toBeNull()
})
