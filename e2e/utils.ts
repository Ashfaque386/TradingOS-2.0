import { type Page, expect } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import path from 'node:path'

// Build Spec §21-22 hardening pass: these credentials only exist in the
// local dev Postgres this suite runs against (seeded by
// backend/scripts/seed_e2e_users.py, real Argon2-hashed via the app's own
// hash_password() -- see that script's own docstring for why this is not
// a production credential-bootstrap mechanism). Never used against a real
// deployment.
export const E2E_USERS = {
  admin: { email: 'e2e.admin@example.com', password: 'E2eAdminPass!2026' },
  pm: { email: 'e2e.pm@example.com', password: 'E2ePmPass!2026' },
  risk: { email: 'e2e.risk@example.com', password: 'E2eRiskPass!2026' },
} as const

/** Switching personas mid-test (e.g. pm requests, admin decides) needs a
 * clean slate first -- the login page immediately bounces an already-
 * authenticated session back to "/" (see app/login/page.tsx's own
 * redirect effect), which detaches the form mid-click if a stale token
 * from the previous persona is still in localStorage. */
export async function login(page: Page, user: { email: string; password: string }) {
  await page.goto('/login')
  await page.evaluate(() => localStorage.clear())
  await page.reload()
  await page.getByLabel('Email address').fill(user.email)
  await page.getByLabel('Password').fill(user.password)
  await page.getByRole('button', { name: /Initialize session/i }).click()
  await expect(page).toHaveURL('/')
}

const BACKEND_DIR = path.resolve(__dirname, '../backend')
const PYTHON = path.join(BACKEND_DIR, '.venv/bin/python')

const SCRIPT_ENV = {
  ...process.env,
  DATABASE_URL: 'postgresql+asyncpg://tradingos:tradingos@localhost:5432/tradingos',
  JWT_SECRET_KEY: 'dev-only-secret-do-not-use-in-production',
}

/** Runs a backend fixture-seeding script (scripts/seed_e2e_*.py) synchronously
 * and returns its stdout. These scripts exist because reaching some states
 * (a live order intent pending sign-off, three role-specific accounts)
 * through the UI alone would mean re-testing an earlier flow as a
 * prerequisite for every later spec file -- see each script's own
 * docstring for the exact orchestration calls it mirrors. */
export function runSeedScript(scriptName: string, args: string[] = []): string {
  return execFileSync(PYTHON, [`scripts/${scriptName}`, ...args], {
    cwd: BACKEND_DIR,
    env: SCRIPT_ENV,
    encoding: 'utf-8',
  })
}

export function parseSeedOutput(output: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of output.trim().split('\n')) {
    const [key, value] = line.split('=')
    if (key && value !== undefined) result[key] = value
  }
  return result
}

const API_BASE_URL = 'http://localhost:8000'

/** Mission Control's Live Order Intents queue card shows only symbol/side/
 * quantity/type/time -- never the intent id -- so a test can't disambiguate
 * "the one I just seeded" from a stale pending intent left over by an
 * earlier run (a flaky/interrupted test, or manual debugging against this
 * same dev DB) purely from the DOM. Clearing every pending intent before
 * seeding a fresh one keeps each signoff-queue spec deterministic: exactly
 * one DEMOSTOCK card, always the one this test created. */
export async function rejectAllPendingLiveIntents(): Promise<void> {
  const loginRes = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.risk),
  })
  const { access_token: token } = (await loginRes.json()) as { access_token: string }

  const intentsRes = await fetch(`${API_BASE_URL}/api/v1/live-trading/intents`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const intents = (await intentsRes.json()) as { id: string; status: string }[]

  for (const intent of intents.filter((i) => i.status === 'pending_approval')) {
    await fetch(`${API_BASE_URL}/api/v1/live-trading/intents/${intent.id}/reject`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
    })
  }
}
