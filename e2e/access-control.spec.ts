import { expect, test, type Page } from '@playwright/test'
import { API_BASE_URL, E2E_USERS, api, login, tokenFor } from './utils'

// Who can get in, and how: registration is closed once an account exists, accounts are made by an
// administrator, wrong passwords lock the account they were tried on, the API describes itself to nobody,
// and a WebSocket is opened with a one-time ticket instead of the access token.

const PASSWORD = 'AccessPass!2026'
const unique = (label: string) => `e2e.${label}.${Date.now()}@example.com`

async function adminToken() {
  return tokenFor(E2E_USERS.admin)
}

async function createAccount(email: string, role = 'PortfolioManager') {
  const created = await api<{ id: string }>(await adminToken(), '/api/v1/admin/users', {
    method: 'POST',
    body: { email, password: PASSWORD, role },
  })
  expect(created.status).toBe(201)
  return created.body.id
}

async function signIn(email: string, password: string) {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  return { status: res.status, retryAfter: res.headers.get('retry-after'), body: (await res.json()) as { detail?: string } }
}

const row = (page: Page, email: string) =>
  page.getByTestId('users-section').locator(`[data-testid="user-row"][data-email="${email}"]`)

// ---- registration is closed ----

test('the sign-in page offers no way to register once an account exists', async ({ page }) => {
  await page.context().clearCookies()
  await page.goto('/login')

  await expect(page.getByText('Accounts are created by an administrator.')).toBeVisible()
  await expect(page.getByRole('link', { name: /Create the first account/ })).toHaveCount(0)
})

test('the register page says it is closed and has no form to fill in', async ({ page }) => {
  await page.context().clearCookies()
  await page.goto('/register')

  await expect(page.getByRole('status').filter({ hasText: 'Registration is closed' })).toBeVisible()
  await expect(page.getByLabel('Confirm password')).toBeHidden()
})

test('the server refuses a registration whoever sends it, in the same words', async () => {
  const send = (email: string) =>
    fetch(`${API_BASE_URL}/api/v1/auth/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: PASSWORD }),
    })

  const forExisting = await send(E2E_USERS.admin.email)
  const forStranger = await send(unique('stranger'))

  expect(forExisting.status).toBe(403)
  expect(forStranger.status).toBe(403)
  expect(await forExisting.json()).toEqual(await forStranger.json())
})

// ---- the API says nothing about itself ----

test('the API docs are not served', async () => {
  for (const path of ['/docs', '/redoc', '/openapi.json']) {
    expect((await fetch(`${API_BASE_URL}${path}`)).status, path).toBe(404)
  }
})

test('API responses tell a browser to trust nothing in them', async () => {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/registration-status`)

  expect(res.headers.get('x-content-type-options')).toBe('nosniff')
  expect(res.headers.get('x-frame-options')).toBe('DENY')
  expect(res.headers.get('content-security-policy')).toContain("default-src 'none'")
  expect(res.headers.get('cache-control')).toBe('no-store')
})

// ---- WebSockets ----

test('a WebSocket is opened with a one-time ticket, never the access token', async ({ page }) => {
  const urls: string[] = []
  // The API's sockets only: the dev server opens its own hot-reload socket (/_next/hmr) too.
  page.on('websocket', (ws) => {
    if (ws.url().includes('/api/v1/')) urls.push(ws.url())
  })

  await login(page, E2E_USERS.admin)
  await expect.poll(() => urls.length, { timeout: 15_000 }).toBeGreaterThan(0)

  for (const url of urls) {
    expect(url, url).toContain('ticket=')
    expect(url, url).not.toContain('token=')
    expect(url, url).not.toMatch(/eyJ/) // no JWT anywhere in it
  }
})

test('a ticket opens one socket; the same ticket, and the access token, open none', async () => {
  const token = await adminToken()
  const ticket = (await api<{ ticket: string }>(token, '/api/v1/auth/ws-ticket', { method: 'POST' })).body.ticket
  const base = API_BASE_URL.replace(/^http/, 'ws') + '/api/v1/ws/activity-feed'
  const open = (query: string) =>
    new Promise<'open' | 'refused'>((resolve) => {
      const socket = new WebSocket(`${base}?${query}`)
      socket.onopen = () => {
        socket.close()
        resolve('open')
      }
      socket.onerror = () => resolve('refused')
      socket.onclose = () => resolve('refused')
    })

  expect(await open(`ticket=${ticket}`)).toBe('open')
  expect(await open(`ticket=${ticket}`)).toBe('refused')
  expect(await open(`token=${token}`)).toBe('refused')
  expect(await open('')).toBe('refused')
})

test('a ticket cannot be had without signing in', async () => {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/ws-ticket`, { method: 'POST' })

  expect([401, 403]).toContain(res.status)
})

// ---- wrong passwords lock the account ----

test('wrong passwords lock the account, the sign-in page says so, and an administrator unlocks it', async ({ page }) => {
  const email = unique('lock')
  const id = await createAccount(email)

  await page.context().clearCookies()
  await page.goto('/login')
  for (let attempt = 1; attempt <= 5; attempt++) {
    await page.getByLabel('Email address').fill(email)
    await page.getByLabel('Password').fill(`wrong-${attempt}`)
    await page.getByRole('button', { name: /Initialize session/i }).click()
    if (attempt < 5) {
      await expect(page.getByRole('alert').filter({ hasText: 'Invalid credentials' })).toBeVisible()
    }
  }
  await expect(page.getByRole('alert').filter({ hasText: /locked after too many wrong passwords/ })).toBeVisible()

  // Even the right password is refused during the lock, and the answer says how long.
  const during = await signIn(email, PASSWORD)
  expect(during.status).toBe(423)
  expect(Number(during.retryAfter)).toBeGreaterThan(0)

  const unlocked = await api(await adminToken(), `/api/v1/admin/users/${id}/unlock`, { method: 'POST' })
  expect(unlocked.status).toBe(200)
  expect((await signIn(email, PASSWORD)).status).toBe(200)
})

test('unlocking from Settings > Users shows who is locked and lets them back in', async ({ page }) => {
  const email = unique('unlock')
  await createAccount(email)
  for (let attempt = 0; attempt < 5; attempt++) await signIn(email, 'not-the-password')

  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=users')
  const target = row(page, email)
  await expect(target).toHaveAttribute('data-state', 'locked', { timeout: 15_000 })
  await expect(target.getByTestId('user-state')).toContainText('Locked,')

  await target.getByRole('button', { name: 'Unlock' }).click()

  await expect(page.getByTestId('users-result')).toContainText(`${email} can sign in again.`)
  await expect(target).toHaveAttribute('data-state', 'active')
  expect((await signIn(email, PASSWORD)).status).toBe(200)
})

// ---- Settings > Users ----

test('an administrator creates an account, changes its role, deactivates it and brings it back', async ({ page }) => {
  const email = unique('person')
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=users')
  const section = page.getByTestId('users-section')
  await expect(section.getByTestId('user-row').first()).toBeVisible({ timeout: 15_000 })

  const form = section.getByRole('form', { name: 'Create an account' })
  await form.getByLabel('Email address').fill(email)
  await form.getByLabel('First password').fill(PASSWORD)
  await form.getByRole('combobox').selectOption('RiskManager') // its label's name includes the chosen option
  await form.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByTestId('users-result')).toContainText(`Created ${email} as Risk Manager`)
  await expect(row(page, email)).toHaveAttribute('data-state', 'active')
  expect((await signIn(email, PASSWORD)).status).toBe(200)

  await row(page, email).getByLabel(`Role of ${email}`).selectOption('ReadOnlyAuditor')
  await expect(page.getByTestId('users-result')).toContainText(`${email} is now Read Only Auditor.`)

  await row(page, email).getByRole('button', { name: 'Deactivate' }).click()
  const dialog = page.getByRole('dialog')
  await expect(dialog).toContainText(`Deactivate ${email}`)
  await expect(dialog.getByRole('button', { name: 'Deactivate' })).toBeDisabled()
  await dialog.getByLabel(`Type ${email} to confirm`).fill(email)
  await dialog.getByRole('button', { name: 'Deactivate' }).click()
  await expect(dialog).toBeHidden()
  await expect(row(page, email)).toHaveAttribute('data-state', 'deactivated')
  expect((await signIn(email, PASSWORD)).status).toBe(401)

  await row(page, email).getByRole('button', { name: 'Reactivate' }).click()
  await expect(row(page, email)).toHaveAttribute('data-state', 'active')
  expect((await signIn(email, PASSWORD)).status).toBe(200)
})

test('signing a person out everywhere ends their session', async ({ page, browser }) => {
  const email = unique('signout')
  await createAccount(email)
  const theirs = await (await browser.newContext()).newPage()
  await login(theirs, { email, password: PASSWORD })
  await theirs.reload()
  await expect(theirs).toHaveURL('/')

  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=users')
  await row(page, email).getByRole('button', { name: 'Sign out everywhere' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Sign out everywhere' }).click()
  await expect(page.getByTestId('users-result')).toContainText(`of ${email} ended.`)

  await theirs.goto('/strategies')
  await expect(theirs).toHaveURL(/\/login$/, { timeout: 15_000 })
})

test('the last administrator cannot be demoted or deactivated, and the page says why', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=users')
  const own = row(page, E2E_USERS.admin.email)
  await expect(own).toBeVisible({ timeout: 15_000 })

  await own.getByLabel(`Role of ${E2E_USERS.admin.email}`).selectOption('ReadOnlyAuditor')
  await expect(page.getByTestId('users-result')).toContainText('last active SystemAdministrator')
  await expect(own.getByLabel(`Role of ${E2E_USERS.admin.email}`)).toHaveValue('SystemAdministrator')

  await own.getByRole('button', { name: 'Deactivate' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel(`Type ${E2E_USERS.admin.email} to confirm`).fill(E2E_USERS.admin.email)
  await dialog.getByRole('button', { name: 'Deactivate' }).click()
  await expect(dialog.getByRole('alert')).toContainText('last active SystemAdministrator')
})

test('anyone else sees the section but no accounts and no buttons', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/settings?section=users')

  await expect(page.getByTestId('users-section')).toContainText('Read-only — SystemAdministrator role required.')
  await expect(page.getByTestId('user-row')).toHaveCount(0)
  await expect(page.getByRole('form', { name: 'Create an account' })).toHaveCount(0)
})

// ---- Settings > Security ----

test('Security says who is signed in and what the sign-in rules are', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/settings?section=security')
  const policy = page.getByTestId('sign-in-policy')

  await expect(policy.getByTestId('signed-in-as')).toContainText(E2E_USERS.pm.email)
  await expect(policy.getByTestId('signed-in-as')).toContainText('Portfolio Manager')
  await expect(policy.getByTestId('session-rule')).toContainText('renewed every 15 minutes')
  await expect(policy.getByTestId('session-rule')).toContainText('7 days')
  await expect(policy.getByTestId('lock-rule')).toContainText(
    '5 wrong passwords within 15 minutes lock that account for 15 minutes',
  )
})
