import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { API_BASE_URL, E2E_USERS, api, login, tokenFor } from './utils'

// The Audit Log is for the auditor and the administrator only, reads the hash-chained record, and
// lets the administrator re-verify the chain. (Any sign-in is itself an audited action, so there is
// always at least one entry by the time a spec opens the page, and signing in again is how a spec
// makes more.)

const ENTRY_COUNT = /^[1-9][\d,]* (entry|entries)( match)?$/
const MATCH_COUNT = /^[\d,]+ (entry|entries) match$/
const ADMIN = E2E_USERS.admin.email

/** How many entries the server says match, read from the header the page itself relies on. */
async function serverTotal(token: string, query = ''): Promise<number> {
  const res = await fetch(`${API_BASE_URL}/api/v1/audit/entries?limit=1${query}`, { headers: { Authorization: `Bearer ${token}` } })
  return Number(res.headers.get('x-total-count'))
}

/** Makes one entry the administrator is the actor of, about a thing with an id of its own: a new account. */
async function seedEntity(): Promise<string> {
  const created = await api<{ id: string }>(await tokenFor(E2E_USERS.admin), '/api/v1/admin/users', {
    method: 'POST',
    body: { email: `e2e.audit.${Date.now()}@example.com`, password: 'E2eAuditSeed!2026', role: 'ReadOnlyAuditor' },
  })
  expect(created.status).toBe(201)
  return created.body.id
}

test('the read-only auditor reads the log, opens an entry and filters it', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')

  await expect(page.getByRole('heading', { name: 'Audit Log' })).toBeVisible()
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()

  const firstToggle = page.getByRole('button', { name: /^Show details of entry/ }).first()
  await firstToggle.click()
  await expect(page.getByRole('button', { name: /^Hide details of entry/ }).first()).toHaveAttribute('aria-expanded', 'true')
  // The two hashes that chain the entry to the one before it are shown whole, 64 characters each.
  await expect(page.getByText('Previous hash')).toBeVisible()
  await expect(page.getByText('This entry’s hash')).toBeVisible()
  await expect(page.locator('.audit-hash code').first()).toHaveText(/^[0-9a-f]{64}$/)

  await page.getByPlaceholder('Search entity id, actor, action...').fill('no-such-entry-anywhere')
  await expect(page.getByText('No audit entries match these filters.')).toBeVisible()
  await expect(page.getByText('0 entries match')).toBeVisible()

  await page.getByRole('button', { name: 'Clear the filters' }).click()
  await expect(page.getByText('No audit entries match these filters.')).toHaveCount(0)
  await expect(page.getByPlaceholder('Search entity id, actor, action...')).toHaveValue('')
})

test('the log is paged on the server: every page, in order, to the very last entry', async ({ page }) => {
  for (let i = 0; i < 60; i += 1) await tokenFor(E2E_USERS.auditor) // each sign-in is one more entry
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()

  await expect(page.getByText(/^Showing 1–25 of [\d,]+$/)).toBeVisible()
  await expect(page.locator('.audit-table tbody tr:not(.audit-diff-row)')).toHaveCount(25)
  const firstOnPageOne = await page.locator('.audit-table tbody tr td:last-child').first().innerText()

  // Wait for the second page's own request, so a slow runner shows as "the request never came back"
  // rather than as a missing line of text.
  const secondPage = page.waitForResponse((r) => r.url().includes('/audit/entries') && r.url().includes('offset=25'), { timeout: 30_000 })
  await page.getByRole('button', { name: 'Next', exact: true }).click()
  expect((await secondPage).status()).toBe(200)
  await expect(page.getByText(/^Showing 26–50 of [\d,]+$/)).toBeVisible()
  const firstOnPageTwo = await page.locator('.audit-table tbody tr td:last-child').first().innerText()
  // Newest first, so the second page starts with an older entry than the first page did.
  expect(Number(firstOnPageTwo.replace('#', ''))).toBeLessThan(Number(firstOnPageOne.replace('#', '')))

  await page.getByRole('button', { name: 'Last', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Next', exact: true })).toBeDisabled()
  await expect(page.getByText(/^Not refreshing on this page/)).toBeVisible()
  await page.getByRole('button', { name: 'First', exact: true }).click()
  await expect(page.getByText(/^Refreshes every 15 seconds/)).toBeVisible()

  await page.getByLabel('Rows per page').selectOption('100')
  await expect(page.getByText(/^Showing 1–\d+ of [\d,]+$/)).toBeVisible()
  await expect.poll(() => page.locator('.audit-table tbody tr:not(.audit-diff-row)').count()).toBeGreaterThan(25)
})

test('a filter is applied by the server: only that actor’s entries, counted, and kept in the address', async ({ page }) => {
  await seedEntity()
  const token = await tokenFor(E2E_USERS.auditor)
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()

  await page.getByLabel('Actor', { exact: true }).selectOption(ADMIN)

  await expect(page.getByText(MATCH_COUNT)).toBeVisible()
  const rows = page.getByRole('button', { name: /^Summarise / })
  expect(await rows.count()).toBeGreaterThan(0)
  for (const name of await rows.allInnerTexts()) expect(name.trim()).toBe(ADMIN)
  await expect.poll(() => page.url()).toContain(`actor=${encodeURIComponent(ADMIN)}`)
  const expected = await serverTotal(token, `&actor=${encodeURIComponent(ADMIN)}`)
  expect(expected).toBeGreaterThan(0)
  await expect(page.getByText(`${expected.toLocaleString('en-IN')} ${expected === 1 ? 'entry' : 'entries'} match`)).toBeVisible()

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()
  await expect.poll(() => page.url()).not.toContain('actor=')
})

test('a link can open the log already narrowed to one thing', async ({ page }) => {
  const entityId = await seedEntity()

  await login(page, E2E_USERS.auditor)
  await page.goto(`/audit?entity_id=${encodeURIComponent(entityId)}`)

  await expect(page.getByLabel('Entity id')).toHaveValue(entityId)
  await expect(page.getByText('1 entry match')).toBeVisible()
  for (const cell of await page.locator('.audit-table tbody tr td:nth-child(6)').allInnerTexts()) expect(cell.trim()).toBe(entityId)
})

test('an entity opens its whole trace, oldest first, and the log can be narrowed to it', async ({ page }) => {
  const entityId = await seedEntity()
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()
  await page.getByPlaceholder('Search entity id, actor, action...').fill(entityId)
  await expect(page.getByText('1 entry match')).toBeVisible()

  const traceButton = page.getByRole('button', { name: `Trace ${entityId}` })
  await traceButton.click()

  const dialog = page.getByRole('dialog', { name: `Everything recorded for ${entityId}` })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.audit-trace-seq').first()).toBeVisible() // the trace fills when its request answers
  const sequences = (await dialog.locator('.audit-trace-seq').allInnerTexts()).map((s) => Number(s.replace('#', '')))
  expect(sequences.length).toBeGreaterThan(0)
  expect(sequences).toEqual([...sequences].sort((a, b) => a - b)) // oldest first

  await dialog.getByText('Details and hashes').first().click()
  await expect(dialog.locator('.audit-hash code').first()).toHaveText(/^[0-9a-f]{64}$/)

  await page.keyboard.press('Escape')
  await expect(dialog).toHaveCount(0)

  await traceButton.click()
  await page.getByRole('button', { name: 'Show only these in the log' }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByLabel('Entity id')).toHaveValue(entityId)
})

test('an actor opens a summary of what they have done, and the log can be narrowed to them', async ({ page }) => {
  const entityId = await seedEntity()
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()
  await page.getByPlaceholder('Search entity id, actor, action...').fill(entityId)
  await expect(page.getByText('1 entry match')).toBeVisible()

  const actor = ADMIN
  await page.getByRole('button', { name: `Summarise ${actor}` }).click()

  const dialog = page.getByRole('dialog', { name: actor })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText('First seen')).toBeVisible()
  await expect(dialog.getByText('Last seen')).toBeVisible()
  await expect(dialog.getByRole('table', { name: 'Actions, most frequent first' })).toBeVisible()
  await expect(dialog.locator('.audit-share-table tbody td:nth-child(2)').first()).toBeVisible()
  const counts = (await dialog.locator('.audit-share-table tbody td:nth-child(2)').allInnerTexts()).map((c) => Number(c.replace(/,/g, '')))
  expect(counts.length).toBeGreaterThan(0)
  expect(counts).toEqual([...counts].sort((a, b) => b - a)) // most frequent first

  await dialog.getByRole('button', { name: 'Show this actor’s entries' }).click()
  await expect(page.getByLabel('Actor', { exact: true })).toHaveValue(actor)
})

test('an export follows the filters: only what the page is showing is in the file', async ({ page }) => {
  await seedEntity()
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()
  const actor = ADMIN
  await page.getByLabel('Actor', { exact: true }).selectOption(actor)
  await expect(page.getByText(MATCH_COUNT)).toBeVisible()

  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: /Export NDJSON/ }).click()])

  const lines = readFileSync((await download.path()) as string, 'utf-8').trim().split('\n')
  expect(lines.length).toBeGreaterThan(0)
  for (const line of lines) expect((JSON.parse(line) as { actor: string }).actor).toBe(actor)
})

test('a range that ends before it starts is named, not shown as an empty log', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()

  await page.getByLabel('From (India time)').fill('2026-11-02T10:00')
  await page.getByLabel('To (India time)').fill('2026-11-02T09:00')

  await expect(page.getByRole('alert').filter({ hasText: 'The end of the range is before its start.' })).toBeVisible()
})

test('the auditor cannot run the chain verification, the administrator can and sees what was checked', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText('Chain not yet verified this session')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run verification' })).toHaveCount(0)

  await login(page, E2E_USERS.admin)
  await page.goto('/audit')
  await page.getByRole('button', { name: 'Run verification' }).click()
  await expect(page.getByText('Chain not yet verified this session')).toHaveCount(0)

  const result = page.getByRole('list', { name: 'Verification result' })
  await expect(result.getByRole('listitem')).toHaveCount(3)
  await expect(result.getByText('Hash chain in the database: holds')).toBeVisible()
  await expect(result.getByText(/entries checked; every link matches the entry before it/)).toBeVisible()
})

test('the auditor can export the log as a file', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/audit')
  await expect(page.getByText(ENTRY_COUNT)).toBeVisible()

  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: /Export CSV/ }).click(),
  ])

  expect(download.suggestedFilename()).toBe('audit_log_export.csv')
})

test('a portfolio manager is told the log is restricted, and the sidebar does not offer it', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await expect(page.getByRole('navigation', { name: 'Primary' }).getByRole('button', { name: 'Audit Log' })).toHaveCount(0)

  await page.goto('/audit')

  await expect(page.getByRole('heading', { name: 'Access restricted' })).toBeVisible()
  await expect(page.getByRole('table')).toHaveCount(0)
})
