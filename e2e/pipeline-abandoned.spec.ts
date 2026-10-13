import { expect, test } from '@playwright/test'
import { randomUUID } from 'node:crypto'
import { E2E_USERS, login, runSeedScript } from './utils'

// A pipeline run whose process died must not read "running" for ever. The run
// executes inside the HTTP request that started it, so a crash or a redeploy
// leaves no ending: the feed showed a spinner that never resolved and a cancel
// was accepted and never consumed. The backend now sweeps for such runs (at
// startup and every minute) and gives each a `pipeline.abandoned` event.
//
// Two halves, because they need different fixtures:
//  - the sweep: a run planted *before the backend started* (the e2e job seeds it,
//    see ci.yml) must have been ended by the startup sweep -- checked through
//    the API, against the real backend;
//  - the panel: a run already ended that way, planted now so it is the most
//    recent, must render as interrupted with its stuck step marked, not as a
//    live run with a Cancel button.

const API_BASE_URL = 'http://localhost:8000'
// Must match the id the e2e job seeds before starting the backend.
const SEEDED_BEFORE_START = '00000000-0000-4000-8000-0000000000e2'

async function tokenFor(user: { email: string; password: string }): Promise<string> {
  const res = await fetch(`${API_BASE_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(user),
  })
  return ((await res.json()) as { access_token: string }).access_token
}

test('a run left behind before the backend started was ended by its startup sweep', async () => {
  const token = await tokenFor(E2E_USERS.admin)
  const headers = { Authorization: `Bearer ${token}` }

  const res = await fetch(`${API_BASE_URL}/api/v1/agents/pipeline/events?run_id=${SEEDED_BEFORE_START}&limit=50`, { headers })
  expect(res.ok).toBe(true)
  const events = (await res.json()) as { event_type: string; node: string | null; payload: Record<string, unknown> | null }[]

  expect(events.map((e) => e.event_type)).toEqual([
    'pipeline.started',
    'step.started',
    'step.completed',
    'step.started',
    'pipeline.abandoned',
  ])
  const ending = events[events.length - 1]
  expect(ending.node).toBe('market_analysis_step') // where it stopped
  expect(ending.payload?.steps_completed).toBe(1)

  // And a cancel is refused, not accepted and never consumed.
  const cancel = await fetch(`${API_BASE_URL}/api/v1/agents/pipeline/runs/${SEEDED_BEFORE_START}/cancel`, {
    method: 'POST',
    headers,
  })
  expect(cancel.status).toBe(409)
})

test('the panel shows an interrupted run as interrupted: stuck step marked, no cancel offered', async ({ page }) => {
  const objective = `E2E interrupted run ${Date.now()}`
  runSeedScript('seed_e2e_pipeline_run.py', ['--run-id', randomUUID(), '--objective', objective, '--abandoned'])

  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  const panel = page.getByLabel('Agent pipeline steps')

  await expect(panel.getByText(`Objective: ${objective}`)).toBeVisible()
  // exact: the objective and the stuck step's error both contain "interrupted" too
  await expect(panel.getByText('INTERRUPTED', { exact: true })).toBeVisible()
  // The step that was in flight is not a spinner any more.
  const stuck = panel.locator('[data-step-status="failed"]').filter({ hasText: 'market_analysis_step' })
  await expect(stuck).toContainText('interrupted')
  await expect(panel.locator('[data-step-status="running"]')).toHaveCount(0)
  await expect(panel.getByRole('button', { name: 'Cancel pipeline run' })).toHaveCount(0)
})

test('the panel shows a step cancelled mid-flight as cancelled, not as done or failed', async ({ page }) => {
  // A real mid-step cancel needs an LLM call slow enough to interrupt, which CI's stack does not
  // have (the backend tests hold a node open for this); so the events it writes are planted.
  const objective = `E2E cancelled mid-step ${Date.now()}`
  runSeedScript('seed_e2e_pipeline_run.py', ['--run-id', randomUUID(), '--objective', objective, '--cancelled'])

  await login(page, E2E_USERS.admin)
  await page.goto('/mission-control')
  const panel = page.getByLabel('Agent pipeline steps')

  await expect(panel.getByText(`Objective: ${objective}`)).toBeVisible()
  await expect(panel.getByText('CANCELLED', { exact: true })).toBeVisible()
  const aborted = panel.locator('[data-step-status="cancelled"]')
  await expect(aborted).toHaveCount(1)
  await expect(aborted).toContainText('market_analysis_step')
  await expect(aborted).toContainText('stopped before it finished')
  // The step that finished before the cancel is still done; nothing is left spinning.
  await expect(panel.locator('[data-step-status="done"]')).toHaveCount(1)
  await expect(panel.locator('[data-step-status="running"]')).toHaveCount(0)
  await expect(panel.getByRole('button', { name: 'Cancel pipeline run' })).toHaveCount(0)
})
