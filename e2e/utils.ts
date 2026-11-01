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
  auditor: { email: 'e2e.auditor@example.com', password: 'E2eAuditorPass!2026' },
} as const

export const API_BASE_URL = 'http://localhost:8000'

/** A bearer token for a fixture user, straight from the API (no browser). */
export async function tokenFor(user: { email: string; password: string }): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  })
  return ((await res.json()) as { access_token: string }).access_token
}

/** Calls the API as a fixture user's token and returns the parsed body (the status is checked by the caller's expectations). */
export async function api<T>(token: string, path: string, init: { method?: string; body?: unknown } = {}): Promise<{ status: number; body: T }> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    method: init.method ?? 'GET',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
  })
  const text = await res.text()
  return { status: res.status, body: (text ? JSON.parse(text) : null) as T }
}

/** Signs in through the sign-in page. Switching personas mid-test (e.g. pm requests, admin decides)
 * needs a clean slate first: the refresh cookie of the previous persona would restore that session
 * on page load, and the sign-in page immediately bounces an already-authenticated session back to "/"
 * (see app/login/page.tsx's own redirect effect), which detaches the form mid-click. So the browser
 * context's cookies are cleared before the page is opened. */
export async function login(page: Page, user: { email: string; password: string }) {
  await page.context().clearCookies()
  await page.goto('/login')
  await page.evaluate(() => {
    try {
      localStorage.clear()
    } catch {
      // storage unavailable: nothing to clear
    }
  })
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
