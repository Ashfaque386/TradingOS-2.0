import { type Route, expect, test } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// A page load restores the session: it has no access token (that lives in memory only), so it presents the
// refresh cookie to /auth/refresh and then asks /auth/me who it is. A failure there can mean two very
// different things, and the app must tell them apart: the server answered "this session is over" (401 --
// go to /login), or it never got to answer (rate-limited, erroring, unreachable -- the session may be
// fine, so the operator must NOT be signed out). Found when the blanket API rate limiter's 429s sent a
// valid session to /login in CI; see docs/CLAUDE.md's Phase 26 follow-up.

const REFRESH_COOKIE = 'tradingos_refresh'

function fulfillJson(route: Route, status: number, body: unknown) {
  const origin = route.request().headers()['origin'] ?? '*'
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'access-control-allow-origin': origin,
      'access-control-allow-credentials': 'true',
      'access-control-allow-headers': '*',
      'access-control-allow-methods': '*',
    },
    body: JSON.stringify(body),
  })
}

const refreshCookie = async (page: import('@playwright/test').Page) =>
  (await page.context().cookies()).find((c) => c.name === REFRESH_COOKIE)

test('a page reload restores the session from the cookie, with nothing stored for a script to find', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.reload()

  await expect(page).toHaveURL('/')
  await expect(page.getByRole('button', { name: E2E_USERS.admin.email, exact: true })).toBeVisible()

  // The long-lived credential is an HttpOnly cookie scoped to the auth routes ...
  const cookie = await refreshCookie(page)
  expect(cookie?.httpOnly).toBe(true)
  expect(cookie?.path).toBe('/api/v1/auth')
  expect(cookie?.sameSite).toBe('Strict')
  // ... so no script can read it, and nothing else holds a token.
  expect(await page.evaluate(() => document.cookie)).not.toContain(REFRESH_COOKIE)
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toMatch(/token|eyJ/i)
})

test('tokens an earlier version kept in localStorage are removed on load', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.evaluate(() => localStorage.setItem('tradingos.tokens', JSON.stringify({ refreshToken: 'old' })))

  await page.reload()

  await expect(page).toHaveURL('/')
  expect(await page.evaluate(() => localStorage.getItem('tradingos.tokens'))).toBeNull()
})

test('signing out ends the session: the cookie goes and a reload lands on the sign-in page', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  await page.getByRole('button', { name: E2E_USERS.admin.email, exact: true }).click()
  await page.getByRole('button', { name: 'Logout' }).click()
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 })
  expect(await refreshCookie(page)).toBeUndefined()

  await page.goto('/strategies')
  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 })
})

test('two tabs reloading together both stay signed in', async ({ page, context }) => {
  await login(page, E2E_USERS.admin)
  const other = await context.newPage()
  await other.goto('/strategies')
  await expect(other).toHaveURL(/\/strategies$/)

  // Each refresh retires the token it presents; two tabs presenting the same one would look like a replay
  // and end the session for both. They take turns instead.
  await Promise.all([page.reload(), other.reload()])

  await expect(page).toHaveURL('/')
  await expect(other).toHaveURL(/\/strategies$/)
  await expect(other.getByRole('button', { name: E2E_USERS.admin.email, exact: true })).toBeVisible()
})

test('a session survives the session restore failing with no response at all', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  // What a 429 from the rate limiter looks like to the browser: it carries no CORS headers, so fetch()
  // rejects outright instead of returning a status.
  let refreshCalls = 0
  await page.route('**/api/v1/auth/refresh', (route) => {
    refreshCalls += 1
    return refreshCalls <= 2 ? route.abort('failed') : route.continue()
  })

  await page.goto('/strategies')
  await expect.poll(() => refreshCalls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3)

  await expect(page).toHaveURL(/\/strategies$/)
  expect(await refreshCookie(page)).toBeDefined()
})

test('a session survives the restore answering 429 or 503', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  let refreshCalls = 0
  await page.route('**/api/v1/auth/refresh', (route) => {
    refreshCalls += 1
    if (refreshCalls === 1) return fulfillJson(route, 429, { detail: 'rate limited' })
    if (refreshCalls === 2) return fulfillJson(route, 503, { detail: 'unavailable' })
    return route.continue()
  })

  await page.goto('/strategies')
  await expect.poll(() => refreshCalls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3)

  await expect(page).toHaveURL(/\/strategies$/)
  expect(await refreshCookie(page)).toBeDefined()
})

test('a session survives the identity check failing after the restore', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  let meCalls = 0
  await page.route('**/api/v1/auth/me', (route) => {
    meCalls += 1
    return meCalls <= 2 ? route.abort('failed') : route.continue()
  })

  await page.goto('/strategies')
  await expect.poll(() => meCalls, { timeout: 15_000 }).toBeGreaterThanOrEqual(3)

  await expect(page).toHaveURL(/\/strategies$/)
})

test('an unreachable server shows a retry screen, then recovers without a new login', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  let failing = true
  await page.route('**/api/v1/auth/refresh', (route) => (failing ? route.abort('failed') : route.continue()))

  await page.goto('/strategies')

  // Retries run out, so the shell stops waiting silently -- but it must not redirect: the cookie is still good.
  await expect(page.getByText("Can't reach the TradingOS server")).toBeVisible({ timeout: 20_000 })
  await expect(page).toHaveURL(/\/strategies$/)
  expect(await refreshCookie(page)).toBeDefined()

  failing = false
  await page.getByRole('button', { name: 'Retry now' }).click()

  await expect(page.getByText("Can't reach the TradingOS server")).toBeHidden({ timeout: 15_000 })
  await expect(page).toHaveURL(/\/strategies$/)
})

test('a genuinely rejected session goes to the login screen', async ({ page }) => {
  await login(page, E2E_USERS.admin)

  await page.route('**/api/v1/auth/refresh', (route) =>
    fulfillJson(route, 401, { detail: 'Invalid or expired refresh token' }),
  )

  await page.goto('/strategies')

  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 })
})

test('a session the server really ended is refused, and the dead cookie is dropped', async ({ page }) => {
  await login(page, E2E_USERS.pm)

  // Sign the account out everywhere, as an administrator would, from outside this browser.
  const adminToken = await tokenFor(E2E_USERS.admin)
  const users = await api<Array<{ id: string; email: string }>>(adminToken, '/api/v1/admin/users')
  const pm = users.body.find((u) => u.email === E2E_USERS.pm.email)
  expect(pm).toBeDefined()
  expect((await api(adminToken, `/api/v1/admin/users/${pm!.id}/revoke-sessions`, { method: 'POST' })).status).toBe(200)

  await page.goto('/strategies')

  await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 })
  await expect.poll(() => refreshCookie(page), { timeout: 5_000 }).toBeUndefined()
})
