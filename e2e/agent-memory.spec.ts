import { expect, test } from '@playwright/test'
import { E2E_USERS, login, API_BASE_URL, tokenFor } from './utils'

// Agent long-term vector memory on Mission Control. CI's E2E backend has no
// Qdrant server and no embedding provider, so the state it must show there is
// the honest "unavailable" one with the backend's own reason -- not an empty
// search box that would pretend to work. Against a stack that does have both
// configured (docker-compose with MEMORY_EMBEDDING_* set) the same spec
// exercises the live add / search / delete flow instead. Which one applies is
// read from the API, so the spec is truthful in either environment.


type MemoryStatus = {
  available: boolean
  reason: string | null
  collections: { kind: string; exists: boolean; count: number }[]
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
    await expect(panel(page).getByLabel('New memory', { exact: true })).toHaveCount(0)
  }
})

test('an admin can save the embedding choice, it persists across a reload, and can be reset', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  const form = panel(page).getByLabel('Memory embedding settings')
  await expect(form).toBeVisible()

  // The dev server can re-render this page while a route finishes compiling, which would drop what was
  // typed -- before the Save button is checked, or in the moment between the check and the click (CI
  // runs 86 and 93). So typing, checking and saving are one attempt, and a form found emptied at any of
  // the three steps costs another attempt, not the test's whole timeout. The click gets a short timeout
  // of its own for that reason: a disabled button means the values are gone, so type them again.
  await expect(async () => {
    await form.getByLabel('Embedding provider').selectOption('ollama')
    await form.getByLabel('Embedding model').fill('nomic-embed-text')
    await expect(form.getByRole('button', { name: 'Save' })).toBeEnabled({ timeout: 2_000 })
    await form.getByRole('button', { name: 'Save' }).click({ timeout: 3_000 })
  }).toPass({ timeout: 30_000 })
  await expect(form.getByText(/Saved by e2e\.admin@example\.com/)).toBeVisible()

  // Stored server-side (the database), not in the page: a reload shows it.
  await page.reload()
  await expect(panel(page).getByLabel('Embedding model')).toHaveValue('nomic-embed-text')

  await panel(page).getByRole('button', { name: 'Reset' }).click()
  await expect(panel(page).getByLabel('Memory embedding settings').getByText(/Saved by/)).toHaveCount(0)
})

test('only an admin sees the embedding settings form', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/mission-control')
  await expect(panel(page)).toBeVisible()
  await expect(panel(page).getByLabel('Memory embedding settings')).toHaveCount(0)
})

test('with memory online: add a note, find it by search, delete it', async ({ page }) => {
  test.skip(!(await memoryStatus()).available, 'no Qdrant + embedding provider in this environment')

  const note = `E2E memory note ${Date.now()}: momentum fails in sideways markets`
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')

  const newMemory = panel(page).getByLabel('New memory', { exact: true })
  await newMemory.fill(note)
  await panel(page).getByRole('button', { name: 'Remember' }).click()
  // The panel clears the field only once the memory has actually been stored
  // (embedded + written), so wait for that before searching -- otherwise the
  // search can run before the write lands and honestly find nothing.
  await expect(newMemory).toHaveValue('')

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
  await expect(panel(page).getByLabel('New memory', { exact: true })).toHaveCount(0)
})

test('with memory online: running the pipeline records nothing in memory', async () => {
  // The first version of memory wrote down every run's backtest, evaluation and
  // deployment as lessons -- all placeholders (the feed marks them "simulated"),
  // so each run taught later ones a fabricated outcome. A normal run now leaves
  // memory exactly as it found it; only a failed code generation is ever recorded.
  test.skip(!(await memoryStatus()).available, 'no Qdrant + embedding provider in this environment')

  const lessons = async () =>
    (await memoryStatus()).collections.find((c) => c.kind === 'strategy')?.count ?? 0
  const before = await lessons()

  const token = await tokenFor(E2E_USERS.admin)
  const res = await fetch(`${API_BASE_URL}/api/v1/agents/pipeline/run`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ objective: 'E2E memory: a normal run must not teach memory anything' }),
  })
  expect(res.ok).toBe(true)
  const run = (await res.json()) as { deployment_result: { simulated?: boolean } | null }
  expect(run.deployment_result?.simulated).toBe(true)

  expect(await lessons()).toBe(before)
})
