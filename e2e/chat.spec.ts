import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// Chat: the conversation list and its controls. (Export is covered in wiring-fixes.spec.ts; a real
// model reply needs a connected provider, which the test stack does not have, so what is checked about
// sending is that a failure is said and the page stays usable.)

type Session = { id: string; title: string }

async function newSession(token: string, title: string): Promise<Session> {
  const created = await api<Session>(token, '/api/v1/chat/sessions', { method: 'POST', body: { title } })
  expect(created.status).toBe(201)
  return created.body
}

test('a new chat appears in the list and opens empty', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/chat')

  await page.getByRole('button', { name: 'New chat' }).click()

  await expect(page.getByText('No messages yet. Say hello below.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Export', exact: true })).toBeVisible()
})

test('a conversation can be pinned, found by search, and deleted', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E chat ${Date.now()}`
  const session = await newSession(token, title)

  try {
    await login(page, E2E_USERS.pm)
    await page.goto('/chat')

    await page.getByPlaceholder('Search sessions').fill(title)
    const row = page.locator('div.group', { hasText: title })
    await expect(row).toHaveCount(1)

    // The pin and delete buttons are reachable from the keyboard: they show when the row has focus.
    const pin = row.getByRole('button', { name: 'Pin session' })
    await expect(pin).toHaveCSS('opacity', '0') // hidden until the row is hovered or has focus
    await pin.focus()
    await expect(pin).toHaveCSS('opacity', '1')
    await pin.click()
    await expect(row.getByRole('button', { name: 'Unpin session' })).toBeAttached()

    await page.getByLabel('Pinned only').check()
    await expect(row).toHaveCount(1)
    await page.getByLabel('Pinned only').uncheck()

    await row.getByRole('button', { name: 'Delete session' }).click()
    await expect(page.locator('div.group', { hasText: title })).toHaveCount(0)
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('opening a conversation that cannot be loaded says so instead of showing it empty', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E chat failing ${Date.now()}`
  const session = await newSession(token, title)

  try {
    await login(page, E2E_USERS.pm)
    await page.route(`**/api/v1/chat/sessions/${session.id}/messages`, (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        headers: { 'access-control-allow-origin': route.request().headers()['origin'] ?? '*' },
        body: JSON.stringify({ detail: 'the message store is down' }),
      }),
    )
    await page.goto('/chat')
    await page.getByText(title).first().click()

    await expect(page.getByRole('alert').filter({ hasText: 'the message store is down' })).toBeVisible()
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})
