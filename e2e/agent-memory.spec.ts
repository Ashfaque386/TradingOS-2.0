import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Agent long-term vector memory on Mission Control. CI's E2E backend has no
// Qdrant server and no embedding provider, so the state it must show there is
// the honest "unavailable" one with the backend's own reason -- not an empty
// search box that would pretend to work. Against a stack that does have both
// configured (docker-compose with MEMORY_EMBEDDING_* set) the same spec
// exercises the live add / search / delete flow instead. Which one applies is
// read from the API, so the spec is truthful in either environment.

const API_BASE_URL = 'http://localhost:8000'

type MemoryStatus = { available: boolean; reason: string | null }

async function tokenFor(user: { email: string; password: string }): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  })
  return ((await res.json()) as { access_token: string }).access_token
}

async function memoryStatus(): Promise<MemoryStatus> {
  const token = await tokenFor(E2E_USERS.admin)
  const res = await fetch(`${API_BASE_URL}/api/v1/memory/status`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  expect(res.ok).toBe(true)
  return (await res.json()) as MemoryStatus
}

const panel = (page: import('@playwright/test').Page) => page.getByLabel('Agent memory')

test('the memory panel reports its real availability and the reason when it is off', async ({ page }) => {
  const status = await memoryStatus()
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  await expect(panel(page)).toBeVisible()

  if (status.available) {
    await expect(panel(page).getByText('ONLINE')).toBeVisible()
    await expect(panel(page).getByLabel('Memory search')).toBeVisible()
  } else {
    await expect(panel(page).getByText('UNAVAILABLE')).toBeVisible()
    await expect(panel(page).getByRole('status')).toContainText('Memory is not available')
    // The backend's own reason, verbatim -- it names the setting to fix.
    await expect(panel(page).getByRole('status')).toContainText(status.reason ?? '')
    // No controls that could only fail.
    await expect(panel(page).getByLabel('Memory search')).toHaveCount(0)
    await expect(panel(page).getByLabel('New memory')).toHaveCount(0)
  }
})

test('with memory online: add a note, find it by search, delete it', async ({ page }) => {
  test.skip(!(await memoryStatus()).available, 'no Qdrant + embedding provider in this environment')

  const note = `E2E memory note ${Date.now()}: momentum fails in sideways markets`
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')

  await panel(page).getByLabel('New memory').fill(note)
  await panel(page).getByRole('button', { name: 'Remember' }).click()

  await panel(page).getByLabel('Memory search').fill('momentum sideways markets')
  await panel(page).getByRole('button', { name: 'Search' }).click()
  const row = panel(page).locator('[data-memory-kind]').filter({ hasText: note })
  await expect(row).toBeVisible({ timeout: 15_000 })

  await row.getByRole('button', { name: 'Delete memory' }).click()
  await expect(row).toHaveCount(0)
})

test('with memory online: a read-only role can search but not add or delete', async ({ page }) => {
  test.skip(!(await memoryStatus()).available, 'no Qdrant + embedding provider in this environment')

  await login(page, E2E_USERS.risk)
  await page.goto('/mission-control')

  await expect(panel(page).getByLabel('Memory search')).toBeVisible()
  await expect(panel(page).getByLabel('New memory')).toHaveCount(0)
})
