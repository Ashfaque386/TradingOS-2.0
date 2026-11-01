import { expect, test } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { E2E_USERS, api, login, tokenFor } from './utils'

// Settings > System and backups: the Postgres backup, made for real (pg_dump, the manifest, the check)
// through the real API, and what the page says about it. The backend's folder is emptied first so the
// "nothing yet" warning is the true starting point; the specs run in order and share what they make.

const BACKUPS_DIR = path.resolve(__dirname, '../backend/backups')

test.describe.configure({ mode: 'serial' })

test.beforeAll(() => {
  fs.rmSync(BACKUPS_DIR, { recursive: true, force: true })
})

test.afterAll(() => {
  fs.rmSync(BACKUPS_DIR, { recursive: true, force: true })
})

const section = (page: import('@playwright/test').Page) => page.getByTestId('backups-section')

test('with nothing backed up, the page says there is nothing to restore from', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=backups')

  const summary = section(page).getByTestId('backup-summary-postgres')
  await expect(summary).toContainText('No database backup has been made, so there is nothing to restore the database from.')
  await expect(summary).toHaveAttribute('data-tone', 'warn')
  await expect(section(page).getByTestId('backups-postgres').getByTestId('backup-row')).toHaveCount(0)
  await expect(section(page).getByTestId('backup-schedule')).toContainText('Postgres every day at 22:45 IST')
})

test('Back up now makes a real, checked backup and the page shows it', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=backups')

  await section(page).getByTestId('backup-now').click()

  await expect(section(page).getByTestId('backup-result')).toContainText(/^Backed up as \d{8}T\d{6}Z \(.+, checked\)\./, { timeout: 120_000 })
  const summary = section(page).getByTestId('backup-summary-postgres')
  await expect(summary).toHaveAttribute('data-tone', 'ok')
  await expect(summary).toContainText(/The newest database backup is from (under a minute|\d+ min) ago\./)

  const rows = section(page).getByTestId('backups-postgres').getByTestId('backup-row')
  await expect(rows).toHaveCount(1)
  await expect(rows.first()).toHaveAttribute('data-status', 'ok')
  await expect(rows.first()).toContainText(/\d+ tables, [\d,]+ rows, audit (entry \d+|log empty)/)
  await expect(rows.first().locator('code').first()).toHaveText(/^[0-9a-f]{12}$/)
  await expect(rows.first()).toContainText('Usable')

  // On disk: the dump and its manifest, and nothing half-written left behind.
  const files = fs.readdirSync(BACKUPS_DIR).sort()
  expect(files).toHaveLength(2)
  expect(files[0]).toMatch(/^postgres-\d{8}T\d{6}Z\.dump$/)
  expect(files[1]).toMatch(/^postgres-\d{8}T\d{6}Z\.manifest\.json$/)
  const manifest = JSON.parse(fs.readFileSync(path.join(BACKUPS_DIR, files[1]), 'utf-8')) as { sha256: string; tables: Record<string, number> }
  expect(manifest.sha256).toMatch(/^[0-9a-f]{64}$/)
  expect(Object.keys(manifest.tables)).toContain('audit_log')
  // The dump holds the signing keys and password hashes: owner-only.
  expect(fs.statSync(path.join(BACKUPS_DIR, files[0])).mode & 0o077).toBe(0)
})

test('a dump that never finished is listed as not usable, and the page says the latest attempt failed', async ({ page }) => {
  const later = new Date(Date.now() + 60_000).toISOString().replace(/[-:]/g, '').replace(/\.\d+/, '')
  const unfinished = path.join(BACKUPS_DIR, `postgres-${later}.dump`)
  fs.writeFileSync(unfinished, 'half a dump')
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=backups')

    const rows = section(page).getByTestId('backups-postgres').getByTestId('backup-row')
    await expect(rows).toHaveCount(2)
    await expect(rows.first()).toHaveAttribute('data-status', 'incomplete')
    await expect(rows.first()).toContainText('the dump has no manifest: it did not finish')
    await expect(rows.first()).toContainText('Not usable')
    await expect(rows.nth(1)).toHaveAttribute('data-status', 'ok')
    await expect(section(page).getByTestId('backup-summary-postgres')).toContainText('The latest attempt did not finish and cannot be restored from.')
  } finally {
    fs.rmSync(unfinished, { force: true })
  }
})

test('the data lake has its own line, judged on its own', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=backups')

  await expect(section(page).getByTestId('backups-lake')).toBeVisible()
  await expect(section(page).getByTestId('backup-summary-lake')).toBeVisible()
  await expect(section(page).getByTestId('backup-summary-postgres')).toHaveAttribute('data-tone', 'ok')
})

test('the page reminds an administrator what no backup holds', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=backups')

  const reminder = section(page).getByTestId('key-custody')
  await expect(reminder).toContainText('SECRETS_ENCRYPTION_KEY')
  await expect(reminder).toContainText('cannot be read after a restore')
  await expect(reminder).toContainText('same machine as the database')
})

test('anyone else sees the section but no backups and no button, and the API refuses them', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/settings?section=backups')

  await expect(section(page)).toContainText('SystemAdministrator role required')
  await expect(page.getByTestId('backup-now')).toHaveCount(0)
  await expect(page.getByTestId('backups-postgres')).toHaveCount(0)
  await expect(page.getByTestId('key-custody')).toHaveCount(0)

  for (const user of [E2E_USERS.pm, E2E_USERS.auditor]) {
    const token = await tokenFor(user)
    expect((await api(token, '/api/v1/system/backups')).status).toBe(403)
    expect((await api(token, '/api/v1/system/postgres-backup', { method: 'POST' })).status).toBe(403)
  }
})
