import { expect, test, type Page } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// A scheduled job's schedule, edited in Mission Control, is kept (it used to change the running job
// only, and a restart put the code's schedule back without a word) and can be put back. That a restart
// applies it is proved in the backend tests; here the page: what it says, and the restore.

const SCHEDULER = 'investor_reporting'
const JOB = 'investor_report_weekly_run'

type Job = { scheduler: string; job_id: string; trigger: string; overridden: boolean; default_trigger: string | null }

async function job(token: string): Promise<Job> {
  const { body } = await api<{ jobs: Job[] }>(token, '/api/v1/system/scheduled-jobs')
  return body.jobs.find((j) => j.scheduler === SCHEDULER && j.job_id === JOB)!
}

const row = (page: Page) => page.locator('div.rounded-lg.px-2.py-2', { hasText: JOB })

test('a changed schedule says it is kept across restarts and what the code\'s own is, and can be restored', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.admin)
  const original = (await job(token)).trigger
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/mission-control')

    await row(page).getByRole('button', { name: `Edit schedule of ${JOB}` }).click()
    await row(page).getByPlaceholder('0-23').fill('9')
    await row(page).getByPlaceholder('0-59').fill('45')
    await expect(row(page).getByText('Kept across restarts.')).toBeVisible()
    await row(page).getByRole('button', { name: 'Save', exact: true }).click()

    const note = page.getByTestId(`customised-${SCHEDULER}-${JOB}`)
    await expect(note).toContainText('Chosen by an administrator and kept across restarts')
    await expect(note).toContainText(original) // the code's own schedule, named
    const changed = await job(token)
    expect(changed.overridden).toBe(true)
    expect(changed.default_trigger).toBe(original)
    expect(changed.trigger).toContain("hour='9'")
    expect(changed.trigger).toContain("minute='45'")
    expect(changed.trigger).toContain("day_of_week='mon'") // the other fields of the expression are kept

    await row(page).getByRole('button', { name: `Restore the default schedule of ${JOB}` }).click()
    const dialog = page.getByRole('dialog', { name: `Restore the default schedule of ${JOB}?` })
    await expect(dialog).toContainText(original)
    await dialog.getByRole('button', { name: 'Restore default' }).click()

    await expect(note).toHaveCount(0)
    const restored = await job(token)
    expect(restored.overridden).toBe(false)
    expect(restored.trigger).toBe(original)
  } finally {
    await api(token, `/api/v1/system/scheduled-jobs/${SCHEDULER}/${JOB}/schedule`, { method: 'DELETE' })
  }
})

test('cancelling a restore leaves the chosen schedule in place', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.admin)
  try {
    await api(token, `/api/v1/system/scheduled-jobs/${SCHEDULER}/${JOB}/schedule`, { method: 'PUT', body: { hour: 8, minute: 15 } })
    await login(page, E2E_USERS.admin)
    await page.goto('/mission-control')

    await row(page).getByRole('button', { name: `Restore the default schedule of ${JOB}` }).click()
    await page.getByRole('dialog', { name: /^Restore the default schedule/ }).getByRole('button', { name: 'Cancel' }).click()

    expect((await job(token)).overridden).toBe(true)
    await expect(page.getByTestId(`customised-${SCHEDULER}-${JOB}`)).toBeVisible()
  } finally {
    await api(token, `/api/v1/system/scheduled-jobs/${SCHEDULER}/${JOB}/schedule`, { method: 'DELETE' })
  }
})

test('someone who may not manage jobs sees that a schedule was chosen but has no way to change or restore it', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.admin)
  try {
    await api(token, `/api/v1/system/scheduled-jobs/${SCHEDULER}/${JOB}/schedule`, { method: 'PUT', body: { hour: 8, minute: 15 } })
    await login(page, E2E_USERS.pm)
    await page.goto('/mission-control')

    await expect(page.getByTestId(`customised-${SCHEDULER}-${JOB}`)).toBeVisible()
    await expect(page.getByRole('button', { name: `Edit schedule of ${JOB}` })).toHaveCount(0)
    await expect(page.getByRole('button', { name: `Restore the default schedule of ${JOB}` })).toHaveCount(0)
  } finally {
    await api(token, `/api/v1/system/scheduled-jobs/${SCHEDULER}/${JOB}/schedule`, { method: 'DELETE' })
  }
})
