import { expect, test } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// Cancelling an organization run from Mission Control (backlog item 21).
//
// A cancellable run cannot be produced through the API -- every capability is
// an instant stub, so a run is already COMPLETED when its request returns --
// so backend/scripts/seed_e2e_paused_run.py plants a paused one with a mixed
// task graph (one finished task, one ready, one blocked). Everything after
// that goes through the real UI and API: the confirm step, the cancel request,
// and what each task ends up as.

const API_BASE_URL = 'http://localhost:8000'

type RunDto = { status: string; completed_at: string | null; tasks: { plan_key: string; status: string }[] }

async function seedPausedRun(): Promise<{ runId: string; objective: string }> {
  const parsed = parseSeedOutput(runSeedScript('seed_e2e_paused_run.py'))
  expect(parsed.run_id).toBeTruthy()
  expect(parsed.objective).toBeTruthy()
  return { runId: parsed.run_id, objective: parsed.objective }
}

async function fetchRun(runId: string): Promise<RunDto> {
  const login = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.admin),
  })
  const { access_token } = (await login.json()) as { access_token: string }
  const res = await fetch(`${API_BASE_URL}/api/v1/orchestration/runs/${runId}`, {
    headers: { Authorization: `Bearer ${access_token}` },
  })
  return (await res.json()) as RunDto
}

async function openRun(page: import('@playwright/test').Page, objective: string) {
  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  await page.getByRole('heading', { level: 3, name: objective, exact: true }).click()
  return page.locator('aside').filter({ hasText: 'RUN INSPECTOR' })
}

test('an operator can cancel a paused run; finished work is kept and the run can be rerun', async ({ page }) => {
  const { runId, objective } = await seedPausedRun()
  const drawer = await openRun(page, objective)

  await expect(drawer.getByText(/· paused$/)).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'Continue' })).toBeVisible()

  // Cancelling asks first, and says what it can and cannot do.
  await drawer.getByRole('button', { name: 'Cancel run' }).click()
  const confirm = drawer.getByRole('alertdialog')
  await expect(confirm).toContainText('cannot be resumed')
  await expect(confirm).toContainText('cannot be interrupted')
  await confirm.getByRole('button', { name: 'Yes, cancel run' }).click()

  // The drawer reflects the new state: no longer pausable/continuable/cancellable, rerunnable.
  await expect(drawer.getByText(/· cancelled$/)).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'Rerun' })).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'Cancel run' })).toHaveCount(0)
  await expect(drawer.getByRole('button', { name: 'Continue' })).toHaveCount(0)

  // And it really happened server-side, task by task.
  const run = await fetchRun(runId)
  expect(run.status).toBe('cancelled')
  expect(run.completed_at).not.toBeNull()
  expect(Object.fromEntries(run.tasks.map((t) => [t.plan_key, t.status]))).toEqual({
    research: 'succeeded', // finished work keeps its outcome
    analysis: 'cancelled',
    backtest: 'cancelled',
  })
})

test('"Keep it" backs out of a cancel and leaves the run paused', async ({ page }) => {
  const { runId, objective } = await seedPausedRun()
  const drawer = await openRun(page, objective)

  await drawer.getByRole('button', { name: 'Cancel run' }).click()
  await drawer.getByRole('alertdialog').getByRole('button', { name: 'Keep it' }).click()

  await expect(drawer.getByRole('alertdialog')).toHaveCount(0)
  await expect(drawer.getByText(/· paused$/)).toBeVisible()
  await expect(drawer.getByRole('button', { name: 'Continue' })).toBeVisible()
  expect((await fetchRun(runId)).status).toBe('paused')
})

test('a run that finishes elsewhere first is reported, not silently ignored', async ({ page }) => {
  const { runId, objective } = await seedPausedRun()
  const drawer = await openRun(page, objective)
  await drawer.getByRole('button', { name: 'Cancel run' }).click()

  // Someone else cancels the same run before this operator confirms.
  const login = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(E2E_USERS.pm),
  })
  const { access_token } = (await login.json()) as { access_token: string }
  const other = await fetch(`${API_BASE_URL}/api/v1/orchestration/runs/${runId}/cancel`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${access_token}` },
  })
  expect(other.status).toBe(200)

  await drawer.getByRole('alertdialog').getByRole('button', { name: 'Yes, cancel run' }).click()

  await expect(drawer.getByRole('alert')).toContainText('only a pending, planning, running or paused run can be cancelled')
  await expect(drawer.getByText(/· cancelled$/)).toBeVisible()
})
