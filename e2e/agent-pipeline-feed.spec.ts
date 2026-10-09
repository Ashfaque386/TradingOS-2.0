import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Phase 19 Finding #1: the live per-step agent feed on Mission Control. Each
// step of a LangGraph pipeline run is recorded by the backend and pushed over
// /ws/agent-pipeline-events; this spec checks the part a unit test can't --
// that a run started *elsewhere* shows up in an already-open panel with no
// refresh (the live path), and that the run form is limited to the roles
// the backend allows.

const API_BASE_URL = 'http://localhost:8000'

async function tokenFor(user: { email: string; password: string }): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  })
  const { access_token } = (await res.json()) as { access_token: string }
  return access_token
}

const panel = (page: import('@playwright/test').Page) => page.getByLabel('Agent pipeline steps')

test('a pipeline run started elsewhere appears live in an already-open panel', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  await expect(panel(page)).toBeVisible()

  // Give the panel's WebSocket a moment to be subscribed before the run
  // starts, so what's asserted below is the live channel, not the history
  // backfill that only runs once on mount.
  await page.waitForTimeout(1500)

  const token = await tokenFor(E2E_USERS.admin)
  const res = await fetch(`${API_BASE_URL}/api/v1/agents/pipeline/run`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ objective: 'E2E live feed: momentum on NIFTY' }),
  })
  expect(res.ok).toBe(true)

  await expect(panel(page).getByText('COMPLETED')).toBeVisible({ timeout: 20_000 })
  await expect(panel(page).getByText('Objective: E2E live feed: momentum on NIFTY')).toBeVisible()
  // The CEO step is first, and shows who answered (the LLM or the
  // deterministic fallback) -- real data from the step's own output.
  const ceo = panel(page).locator('[data-step-status="done"]').filter({ hasText: 'ceo-agent' })
  await expect(ceo).toBeVisible()
  await expect(ceo).toContainText(/ceo_brief: (fallback|llm)/)
  // Every row finished: nothing left spinning.
  await expect(panel(page).locator('[data-step-status="running"]')).toHaveCount(0)
})

test('running the pipeline from the panel shows its steps', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')

  await panel(page).getByLabel('Pipeline objective').fill('E2E from the panel: mean reversion')
  await panel(page).getByRole('button', { name: 'Run pipeline' }).click()

  await expect(panel(page).getByText('Objective: E2E from the panel: mean reversion')).toBeVisible({
    timeout: 30_000,
  })
  await expect(panel(page).getByText('COMPLETED')).toBeVisible({ timeout: 30_000 })
  expect(await panel(page).locator('[data-step-status="done"]').count()).toBeGreaterThan(5)
})

test('a role that cannot run the pipeline sees the panel but no run form', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await page.goto('/mission-control')

  await expect(panel(page)).toBeVisible()
  await expect(panel(page).getByLabel('Pipeline objective')).toHaveCount(0)
  await expect(panel(page).getByRole('button', { name: 'Run pipeline' })).toHaveCount(0)
})
