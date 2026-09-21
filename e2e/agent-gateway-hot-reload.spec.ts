import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { E2E_USERS, login } from './utils'

const API_BASE_URL = 'http://localhost:8000'
const CONFIG_PATH = path.resolve(__dirname, '../config/tradingos.config.json')

// Build Spec §21-22 hardening pass: src/gateway/watcher.py hot-reloads
// config/tradingos.config.json on every filesystem change, re-running the
// exact same apply pipeline the CLI and (later) the API use
// (src/gateway/apply.py's own docstring: "never raises for a bad config;
// only for a genuine infra failure"). This spec hand-edits the real file
// on disk -- the literal hot-reload path, not the Settings page's PUT
// /config editor -- with intentionally invalid JSON5, then confirms via
// the Settings UI that the app both rejects it AND keeps running on the
// last-known-good config, per that file's own top-of-file comment to a
// human operator hand-editing it.
test('an invalid hot-reloaded config is rejected and the app keeps running on the last-known-good version', async ({
  page,
}) => {
  const originalContent = fs.readFileSync(CONFIG_PATH, 'utf-8')

  const loginRes = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.admin),
  })
  const { access_token: token } = (await loginRes.json()) as { access_token: string }

  const beforeRes = await fetch(`${API_BASE_URL}/api/v1/gateway/config`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const before = (await beforeRes.json()) as { version_id: number }

  try {
    // Deliberately malformed: an unterminated string breaks JSON5 parsing
    // outright, before schema validation even gets a chance to run.
    fs.writeFileSync(CONFIG_PATH, '{ version: 1, infra: { broken: "unterminated  ')

    // Poll for the watcher's own version-history row rather than a fixed
    // sleep -- inotify plus this pipeline's DB write is normally
    // sub-second, but never assert on timing you don't control.
    let versions: { id: number; status: string }[] = []
    await expect(async () => {
      const res = await fetch(`${API_BASE_URL}/api/v1/gateway/config/versions`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      versions = (await res.json()) as { id: number; status: string }[]
      expect(versions[0]?.id).toBeGreaterThan(before.version_id)
    }).toPass({ timeout: 15_000 })

    expect(versions[0]!.status).toBe('rejected')

    // The app must still be running on the last-known-good config -- the
    // "Current" version is unchanged, and the resolved config an operator
    // and every agent-facing call site reads is untouched.
    const afterRes = await fetch(`${API_BASE_URL}/api/v1/gateway/config`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    const after = (await afterRes.json()) as { version_id: number; parsed: { version: number } }
    expect(after.version_id).toBe(before.version_id)
    expect(after.parsed.version).toBe(1)

    // Confirmed live in the Settings UI too, not just via the API: the
    // rejected attempt is visible in the version history, and "Current"
    // still points at the pre-existing good version.
    await login(page, E2E_USERS.admin)
    await page.goto('/settings')
    await expect(page.getByText(`v${versions[0]!.id}`)).toBeVisible({ timeout: 10_000 })
    const currentRow = page.locator('li', { has: page.getByText('Current', { exact: true }) })
    await expect(currentRow.getByText(`v${before.version_id}`, { exact: true })).toBeVisible()

    // And the app is genuinely still serving requests, not just this one
    // endpoint -- the whole point of "never raises for a bad config".
    const health = await fetch(`${API_BASE_URL}/health`)
    expect(health.status).toBe(200)
  } finally {
    fs.writeFileSync(CONFIG_PATH, originalContent)
    // Let the watcher pick the restore back up before the next spec runs.
    await expect(async () => {
      const res = await fetch(`${API_BASE_URL}/api/v1/gateway/config/versions`, {
        headers: { Authorization: `Bearer ${token}` },
      })
      const latest = ((await res.json()) as { status: string }[])[0]
      expect(latest?.status).toBe('active')
    }).toPass({ timeout: 15_000 })
  }
})
