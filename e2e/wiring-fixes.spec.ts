import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Defects found by the backend/frontend wiring audit (docs/gap-analysis-2026-10-30.md, section 0), each
// of which showed the operator something untrue or nothing at all:
//
//  - Chat's Export was a plain link to a route that needs a Bearer token, so it opened a 401 page.
//  - Market Analysis opened on "Loading..." forever when the server answered with its reason (503).
//  - The Overview said the kill switch was "OFF · ARMED" when it had not been able to read it.

const API_BASE_URL = 'http://localhost:8000'

async function adminToken(): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.admin),
  })
  return ((await res.json()) as { access_token: string }).access_token
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
