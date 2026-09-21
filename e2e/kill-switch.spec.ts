import { test, expect } from '@playwright/test'
import { E2E_USERS, login } from './utils'

const API_BASE_URL = 'http://localhost:8000'

// Build Spec §21-22 hardening pass: the Kill Switch (Build Spec §8) is
// described in its own module docstring as "the single most important
// safety primitive in this build." Writing this spec surfaced a real,
// shipped gap: src/orchestration/kill_switch.py's resetKillSwitch API
// client function (lib/api.ts) was exported but never called from
// anywhere in the UI -- once tripped, an operator had no way to reset it
// except by calling the API directly. Fixed alongside this spec (see
// app/page.tsx's kill-switch-reset-controls, gated to
// SystemAdministrator/RiskManager, the same roles the backend's
// POST /kill-switch/{mode}/reset endpoint requires).
test('kill switch trips on a real drawdown and a risk manager can reset it from the console', async ({
  page,
}) => {
  // Tripping is driven by a real drawdown observation (POST /check), the
  // same call src.orchestration.kill_switch.check_drawdown's own docstring
  // says "a real equity-monitoring loop will make periodically" -- not a
  // dedicated "trip" button, because none exists (or should exist): the
  // switch trips itself off real numbers, only a human resets it.
  const loginRes = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.risk),
  })
  const { access_token: token } = (await loginRes.json()) as { access_token: string }

  const checkRes = await fetch(`${API_BASE_URL}/api/v1/kill-switch/paper/check`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_equity: 80_000, peak_equity: 100_000 }),
  })
  const tripped = (await checkRes.json()) as { tripped: boolean }
  expect(tripped.tripped).toBe(true)

  await login(page, E2E_USERS.risk)
  await page.goto('/')

  await expect(page.getByText('TRIPPED', { exact: true })).toBeVisible({ timeout: 15_000 })
  const resetButton = page.getByRole('button', { name: /Reset paper kill switch/i })
  await expect(resetButton).toBeVisible()

  await resetButton.click()
  await expect(page.getByText('OFF · ARMED', { exact: true })).toBeVisible({ timeout: 15_000 })
  await expect(page.getByRole('button', { name: /Reset paper kill switch/i })).not.toBeVisible()

  // The reset must be truthfully attributed -- never self-clears, always
  // records who and when (src/orchestration/kill_switch.py's own module
  // docstring).
  const stateRes = await fetch(`${API_BASE_URL}/api/v1/kill-switch/paper`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  const state = (await stateRes.json()) as { tripped: boolean }
  expect(state.tripped).toBe(false)
})

test('an operator without reset rights sees the tripped state but no reset control', async ({
  page,
}) => {
  const loginRes = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.risk),
  })
  const { access_token: token } = (await loginRes.json()) as { access_token: string }
  await fetch(`${API_BASE_URL}/api/v1/kill-switch/paper/check`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ current_equity: 80_000, peak_equity: 100_000 }),
  })

  await login(page, E2E_USERS.pm)
  await page.goto('/')
  await expect(page.getByText('TRIPPED', { exact: true })).toBeVisible({ timeout: 15_000 })
  // PortfolioManager is not in the backend's kill-switch reset RBAC roles
  // (SystemAdministrator, RiskManager only) -- the console must not offer
  // a control this role can't actually use.
  await expect(page.getByRole('button', { name: /Reset paper kill switch/i })).not.toBeVisible()

  // Clean up so this test doesn't leave the shared dev DB's paper switch
  // tripped for whichever spec runs next.
  await fetch(`${API_BASE_URL}/api/v1/kill-switch/paper/reset`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason: 'e2e cleanup' }),
  })
})
