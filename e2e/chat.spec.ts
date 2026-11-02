import { readFileSync } from 'node:fs'
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
    await page.getByRole('dialog', { name: 'Delete this chat?' }).getByRole('button', { name: 'Delete', exact: true }).click()
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
        headers: {
          'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
          // every console request carries credentials, and a browser refuses a faked answer without this
          'access-control-allow-credentials': 'true',
        },
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

function failing(route: import('@playwright/test').Route, detail: string, status = 500) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
      // every console request carries credentials, and a browser refuses a faked answer without this
      'access-control-allow-credentials': 'true',
    },
    body: JSON.stringify({ detail }),
  })
}

async function openChat(page: import('@playwright/test').Page, title: string) {
  await page.goto('/chat')
  await page.getByPlaceholder('Search sessions').fill(title)
  await page.locator('div.group', { hasText: title }).getByRole('button').first().click()
}

test('a chat is renamed in place: Enter saves, Escape leaves it, and a blank title is named, not saved', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E rename ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await openChat(page, title)

    await page.getByRole('button', { name: 'Rename chat' }).click()
    const field = page.getByLabel('Chat title')
    await field.fill('   ')
    await expect(page.getByRole('alert').filter({ hasText: 'A chat needs a title.' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save title' })).toBeDisabled()

    await field.fill('Thrown away')
    await field.press('Escape')
    await expect(field).toHaveCount(0)
    expect((await api<{ title: string }>(token, `/api/v1/chat/sessions/${session.id}`)).body.title).toBe(title)

    await page.getByRole('button', { name: 'Rename chat' }).click()
    await page.getByLabel('Chat title').fill(`  ${title} (renamed)  `)
    await page.getByLabel('Chat title').press('Enter')

    await expect(page.getByText(`${title} (renamed)`).first()).toBeVisible()
    await expect.poll(async () => (await api<{ title: string }>(token, `/api/v1/chat/sessions/${session.id}`)).body.title).toBe(`${title} (renamed)`)
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('which provider answers is chosen per chat, and put back to auto', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E model ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await openChat(page, title)
    const choose = page.getByLabel('Answer with')
    await expect(choose).toHaveValue('')
    expect(await choose.locator('option').count()).toBe(8) // auto and the seven providers
    expect(await choose.locator('option').first().innerText()).toBe('Auto (the fallback order)')

    await choose.selectOption('openai')
    await expect.poll(async () => (await api<{ model: string | null }>(token, `/api/v1/chat/sessions/${session.id}`)).body.model).toBe('openai')
    await expect(page.locator('div.group', { hasText: title })).toContainText('openai')

    await choose.selectOption('')
    await expect.poll(async () => (await api<{ model: string | null }>(token, `/api/v1/chat/sessions/${session.id}`)).body.model).toBeNull()
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('a delete asks first, names the chat, and cancelling keeps it', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E keep ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await page.goto('/chat')
    await page.getByPlaceholder('Search sessions').fill(title)
    const row = page.locator('div.group', { hasText: title })
    await row.getByRole('button', { name: 'Delete session' }).focus()
    await row.getByRole('button', { name: 'Delete session' }).click()

    const dialog = page.getByRole('dialog', { name: 'Delete this chat?' })
    await expect(dialog).toContainText(title)
    await expect(dialog).toContainText('cannot be undone')
    await dialog.getByRole('button', { name: 'Cancel' }).click()

    await expect(dialog).toHaveCount(0)
    await expect(row).toHaveCount(1)
    expect((await api(token, `/api/v1/chat/sessions/${session.id}`)).status).toBe(200)
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('a delete the server refuses stays in the confirmation with its reason', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E refused ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await page.route(`**/api/v1/chat/sessions/${session.id}`, (route) =>
      route.request().method() === 'DELETE' ? failing(route, 'the store is read-only') : route.fallback(),
    )
    await page.goto('/chat')
    await page.getByPlaceholder('Search sessions').fill(title)
    await page.locator('div.group', { hasText: title }).getByRole('button', { name: 'Delete session' }).click()
    const dialog = page.getByRole('dialog', { name: 'Delete this chat?' })
    await dialog.getByRole('button', { name: 'Delete', exact: true }).click()

    await expect(dialog.getByRole('alert')).toContainText('the store is read-only')
    await expect(dialog).toBeVisible()
  } finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('a conversation is exported as Markdown or as JSON, each under the server\'s file name', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E export ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await openChat(page, title)

    const [markdown] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: 'Export', exact: true }).click()])
    expect(markdown.suggestedFilename()).toBe(`chat_${session.id}.md`)
    expect(readFileSync((await markdown.path()) as string, 'utf-8')).toContain(`# ${title}`)

    const [json] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: 'Export JSON' }).click()])
    expect(json.suggestedFilename()).toBe(`chat_${session.id}.json`)
    const parsed = JSON.parse(readFileSync((await json.path()) as string, 'utf-8')) as { id: string; title: string; messages: unknown[] }
    expect(parsed).toMatchObject({ id: session.id, title, messages: [] })
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('a list of chats that cannot be loaded says so, instead of "No chat sessions yet"', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/chat/sessions?*', (route) => failing(route, 'the chat store is down'))
  await page.route('**/api/v1/chat/sessions', (route) =>
    route.request().method() === 'GET' ? failing(route, 'the chat store is down') : route.fallback(),
  )
  await page.goto('/chat')

  const notice = page.getByRole('alert').filter({ hasText: 'Could not load your chats: the chat store is down' })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  await expect(page.getByText('No chat sessions yet')).toHaveCount(0)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('the conversation is a live region a screen reader follows, and says it is busy while a reply streams', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E live ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await openChat(page, title)

    const log = page.getByRole('log', { name: 'Conversation' })
    await expect(log).toHaveAttribute('aria-live', 'polite')
    await expect(log).toHaveAttribute('aria-busy', 'false')
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})

test('times are India\'s', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const title = `E2E time ${Date.now()}`
  const session = await newSession(token, title)
  try {
    await login(page, E2E_USERS.pm)
    await openChat(page, title)

    await expect(page.getByText(/Created by .* · \d{2} \w{3} \d{4}, \d{2}:\d{2}:\d{2} India time/)).toBeVisible()
  } finally {
    await api(token, `/api/v1/chat/sessions/${session.id}`, { method: 'DELETE' })
  }
})
