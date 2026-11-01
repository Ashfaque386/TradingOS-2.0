import { expect, test, type Route } from '@playwright/test'
import { E2E_USERS, login, API_BASE_URL, tokenFor } from './utils'

// Defects found by the backend/frontend wiring audit (docs/gap-analysis-2026-10-30.md, section 0), each
// of which showed the operator something untrue or nothing at all:
//
//  - Chat's Export was a plain link to a route that needs a Bearer token, so it opened a 401 page.
//  - Market Analysis opened on "Loading..." forever when the server answered with its reason (503).
//  - The Overview said the kill switch was "OFF · ARMED" when it had not been able to read it.


async function adminToken(): Promise<string> {
  return tokenFor(E2E_USERS.admin)
}

test('Chat export downloads the conversation instead of opening a 401 page', async ({ page }) => {
  const token = await adminToken()
  const title = `E2E export ${Date.now()}`
  const created = await fetch(`${API_BASE_URL}/api/v1/chat/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ title }),
  })
  expect(created.status).toBe(201)
  const { id } = (await created.json()) as { id: string }
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/chat')
    await page.getByText(title).first().click()

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('button', { name: 'Export', exact: true }).click(),
    ])

    expect(download.suggestedFilename()).toBe(`chat_${id}.md`)
  } finally {
    await fetch(`${API_BASE_URL}/api/v1/chat/sessions/${id}`, {
      method: 'DELETE',
      headers: { Authorization: `Bearer ${token}` },
    })
  }
})

test('Market Analysis says why the pulse is unavailable instead of loading forever', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.route('**/api/v1/market-data/pulse', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'No broker is connected, so there is no market pulse to show.' }),
    }),
  )

  await page.goto('/analysis')

  // Next also renders its own empty role=alert route announcer, so pick ours by its text.
  await expect(page.getByRole('alert').filter({ hasText: 'No broker is connected' })).toBeVisible()
  await expect(page.getByText('Loading market pulse')).toHaveCount(0)
})

test('the Overview does not call a kill switch it could not read "armed"', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.route('**/api/v1/kill-switch/**', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'down' }) }),
  )

  await page.goto('/')

  await expect(page.getByText('UNKNOWN', { exact: true })).toBeVisible()
  await expect(page.getByText('OFF · ARMED')).toHaveCount(0)
})

test('the top bar says degraded and unknown, not healthy and open, when the shell cannot read them', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  const down = (route: Route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'down' }) })
  await page.route('**/api/v1/kill-switch/**', down)
  await page.route('**/api/v1/market-data/market-hours', down)

  await page.goto('/orders')

  const topBar = page.locator('header')
  await expect(topBar.getByText('degraded', { exact: true })).toBeVisible()
  await expect(topBar.getByText('UNKNOWN', { exact: true })).toBeVisible()
  await expect(topBar.getByText('healthy', { exact: true })).toHaveCount(0)
})

test('the pages share one read of the kill switches and market hours instead of each polling its own', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  const seen: Record<string, number> = { paper: 0, live: 0, hours: 0 }
  page.on('request', (request) => {
    const url = request.url()
    if (url.includes('/api/v1/kill-switch/paper') && request.method() === 'GET') seen.paper += 1
    if (url.includes('/api/v1/kill-switch/live') && request.method() === 'GET') seen.live += 1
    if (url.includes('/api/v1/market-data/market-hours')) seen.hours += 1
  })

  await page.goto('/')
  await expect(page.getByText('MARKET HOURS')).toBeVisible()
  await page.waitForTimeout(1500)
  const afterShell = { ...seen }
  expect(afterShell.paper).toBeGreaterThan(0) // the shell did read them (twice in dev, where React mounts effects twice)

  await page.getByRole('navigation', { name: 'Primary' }).getByRole('button', { name: 'Paper Desk' }).click()
  await expect(page.getByRole('heading', { name: 'Paper desk' })).toBeVisible()
  await page.waitForTimeout(1500)

  // Moving to the Paper Desk asked for nothing more: it uses the shell's read, and the shell did not restart.
  expect(seen).toEqual(afterShell)
})

// ---- Slice 2: what the console says when a sign-in is refused, and the pre-trade check ----


function answer(route: Route, status: number, body: unknown, headers: Record<string, string> = {}) {
  const origin = route.request().headers()['origin'] ?? '*'
  const cors = {
    'access-control-allow-origin': origin,
    // every console request carries credentials, and then a wildcard in the next two is read literally
    'access-control-allow-credentials': 'true',
    'access-control-allow-headers': route.request().headers()['access-control-request-headers'] ?? 'content-type',
    'access-control-allow-methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
    'access-control-expose-headers': 'retry-after',
  }
  // The browser sends a preflight first for a JSON POST: let it through, answer the real call.
  if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: { ...cors, ...headers },
    body: JSON.stringify(body),
  })
}

async function trySigningIn(page: import('@playwright/test').Page) {
  await page.goto('/login')
  await page.getByLabel('Email address').fill('someone@example.com')
  await page.getByLabel('Password').fill('not-the-real-password')
  await page.getByRole('button', { name: /Initialize session/i }).click()
}

test('a sign-in refused for too many attempts says so and for how long, not "connectivity"', async ({ page }) => {
  await page.route('**/api/v1/auth/login', (route) =>
    answer(route, 429, { detail: 'Too many requests' }, { 'retry-after': '30' }),
  )

  await trySigningIn(page)

  await expect(page.getByRole('alert').filter({ hasText: 'Too many attempts' })).toContainText('30 seconds')
  await expect(page.getByText('Connectivity error')).toHaveCount(0)
})

test('a sign-in the server fails on says the server had a problem, not that the network is down', async ({ page }) => {
  await page.route('**/api/v1/auth/login', (route) => answer(route, 500, { detail: 'boom' }))

  await trySigningIn(page)

  await expect(page.getByRole('alert').filter({ hasText: 'server had a problem' })).toBeVisible()
  await expect(page.getByText('Connectivity error')).toHaveCount(0)
})

test('the order form is a dry-run pre-trade check: it needs real numbers and sends nothing', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/orders')

  const run = page.getByRole('button', { name: 'Run pre-trade check' })
  await expect(page.getByRole('heading', { name: 'Pre-trade check' })).toBeVisible()
  await expect(run).toBeDisabled() // nothing is pre-filled with a made-up price or portfolio value

  await page.getByLabel('SYMBOL').fill('E2ECHECK')
  await page.getByLabel('QUANTITY').fill('10')
  await page.getByLabel('PROPOSED PRICE').fill('100')
  await page.getByLabel('REFERENCE PRICE').fill('100')
  await page.getByLabel('PORTFOLIO VALUE').fill('1000000')
  await run.click()
  // exact: the panel's intro sentence also says "would be allowed".
  await expect(page.getByText('Would be allowed', { exact: true })).toBeVisible()
  await expect(page.getByText(/passes every paper pre-trade check\. Nothing was sent\./)).toBeVisible()

  // The same order against a tiny portfolio breaks the position limit: refused, with the reason.
  await page.getByLabel('PORTFOLIO VALUE').fill('1000')
  await run.click()
  await expect(page.getByText('Would be refused', { exact: true })).toBeVisible()
  await expect(page.getByText('Would be allowed', { exact: true })).toHaveCount(0)

  await expect(page.getByText('Review LIVE order')).toHaveCount(0)
})
