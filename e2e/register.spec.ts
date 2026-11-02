import { expect, test, type Page, type Route } from '@playwright/test'

// The page that makes the very first account. The real database always has accounts by the time these
// run, so the server's answers are stubbed: what is checked is what the page asks for and what it says
// back. (access-control.spec.ts covers the other half against the real backend: once an account exists
// the page is closed.)

function answer(route: Route, status: number, body: unknown, headers: Record<string, string> = {}) {
  const cors = {
    'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
    // every console request carries credentials, and then a wildcard in the next two is read literally
    'access-control-allow-credentials': 'true',
    'access-control-allow-headers': route.request().headers()['access-control-request-headers'] ?? 'content-type',
    'access-control-allow-methods': 'GET, POST, PUT, PATCH, DELETE, OPTIONS',
    'access-control-expose-headers': 'retry-after',
  }
  if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
  return route.fulfill({ status, contentType: 'application/json', headers: { ...cors, ...headers }, body: JSON.stringify(body) })
}

async function openWithNoAccounts(page: Page) {
  await page.route('**/api/v1/auth/registration-status', (route) =>
    answer(route, 200, { open: true, detail: 'This system has no accounts yet.' }),
  )
  await page.goto('/register')
  await expect(page.getByRole('heading', { name: 'Provision an account.' })).toBeVisible()
}

// The card's own alert: Next's route announcer is a second role=alert on every page.
const alert = (page: Page) => page.getByRole('region', { name: 'Provision an account.' }).getByRole('alert')

async function fill(page: Page, email: string, password: string, confirm: string) {
  await page.getByLabel('Email address').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(confirm)
  await page.getByRole('button', { name: /Create|Provision|Register/i }).first().click()
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('with no accounts the form is offered, and a bad entry is named before anything is sent', async ({ page }) => {
  let sent = 0
  await page.route('**/api/v1/auth/register', (route) => {
    sent += 1
    return answer(route, 201, {})
  })
  await openWithNoAccounts(page)

  await fill(page, 'not-an-email', 'long-enough-1', 'long-enough-1')
  await expect(alert(page)).toContainText('Enter a valid email address.')

  await fill(page, 'first@example.com', 'short', 'short')
  await expect(alert(page)).toContainText('Password must be at least 8 characters.')

  await fill(page, 'first@example.com', 'long-enough-1', 'something-else-1')
  await expect(alert(page)).toContainText('Passwords do not match.')

  expect(sent).toBe(0)
})

test('the first account is created, then signed in with the same credentials', async ({ page }) => {
  const calls: string[] = []
  await page.route('**/api/v1/auth/register', (route) => {
    if (route.request().method() === 'POST') calls.push(`register ${route.request().postData()}`)
    return answer(route, 201, { id: '00000000-0000-4000-8000-000000000001', email: 'first@example.com', role: 'SystemAdministrator', is_active: true })
  })
  await page.route('**/api/v1/auth/login', (route) => {
    if (route.request().method() === 'POST') calls.push(`login ${route.request().postData()}`)
    return answer(route, 200, { access_token: 'stub-token', expires_in: 900 })
  })
  await openWithNoAccounts(page)

  await fill(page, 'first@example.com', 'long-enough-1', 'long-enough-1')

  await expect.poll(() => calls.length).toBe(2)
  const body = JSON.stringify({ email: 'first@example.com', password: 'long-enough-1' })
  expect(calls).toEqual([`register ${body}`, `login ${body}`])
})

test('a refusal from the server is shown in its own words, not as a duplicate address', async ({ page }) => {
  await page.route('**/api/v1/auth/register', (route) =>
    answer(route, 403, { detail: 'Registration is closed: ask an administrator for an account.' }),
  )
  await openWithNoAccounts(page)

  await fill(page, 'late@example.com', 'long-enough-1', 'long-enough-1')

  await expect(alert(page)).toContainText('Registration is closed: ask an administrator for an account.')
  await expect(page.getByText('already exists')).toHaveCount(0)
})

test('an address that is taken says so and points to the sign-in page', async ({ page }) => {
  await page.route('**/api/v1/auth/register', (route) => answer(route, 400, { detail: 'Email already registered' }))
  await openWithNoAccounts(page)

  await fill(page, 'first@example.com', 'long-enough-1', 'long-enough-1')

  await expect(alert(page)).toContainText('An account with this email already exists.')
})

test('too many attempts says how long to wait, and a server fault says it was the server', async ({ page }) => {
  let status = 429
  await page.route('**/api/v1/auth/register', (route) =>
    status === 429 ? answer(route, 429, { detail: 'Too many requests' }, { 'retry-after': '30' }) : answer(route, 500, { detail: 'boom' }),
  )
  await openWithNoAccounts(page)

  await fill(page, 'first@example.com', 'long-enough-1', 'long-enough-1')
  await expect(alert(page)).toContainText('Too many attempts. Try again in 30 seconds.')

  status = 500
  await fill(page, 'first@example.com', 'long-enough-1', 'long-enough-1')
  await expect(alert(page)).toContainText('The server had a problem (500)')
  await expect(page.getByText('Connectivity error')).toHaveCount(0)
})

test('an unreachable server is called a connectivity problem', async ({ page }) => {
  await page.route('**/api/v1/auth/register', (route) => route.abort('connectionrefused'))
  await openWithNoAccounts(page)

  await fill(page, 'first@example.com', 'long-enough-1', 'long-enough-1')

  await expect(alert(page)).toContainText('Connectivity error')
})
