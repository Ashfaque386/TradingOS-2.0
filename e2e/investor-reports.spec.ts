import { readFileSync } from 'node:fs'
import { expect, test, type Route } from '@playwright/test'
import { addDays, todayInIndia } from '../lib/dates'
import { defaultPeriod } from '../lib/investor-report-period'
import { E2E_USERS, login } from './utils'

// Investor reports: a portfolio manager generates one for a period the server will accept, reads it as a
// document, saves it as a file and finds it again from its address; a read-only role can read the
// history but is not offered the form; and a history that cannot be loaded is said so to everyone.

function respond(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
      // every console request carries credentials, and a browser refuses a faked answer without this
      'access-control-allow-credentials': 'true',
    },
    body: JSON.stringify(body),
  })
}

// A Monday-to-Sunday week of its own, long past, chosen at random so a rerun against the same database
// does not find the report an earlier run wrote.
function freshWeek(): { start: string; end: string } {
  const start = addDays('2010-01-04', Math.floor(Math.random() * 400) * 7)
  return { start, end: addDays(start, 6) }
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('a portfolio manager generates a report and finds it in the history', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  await expect(page.getByRole('heading', { name: 'Investor reporting' })).toBeVisible()

  // A period of its own, so the entry cannot be confused with another spec's or the scheduler's.
  const start = '2019-03-04'
  const end = '2019-03-10'
  await page.getByLabel('Period start').fill(start)
  await page.getByLabel('Period end').fill(end)
  await page.getByLabel('Cadence').selectOption('weekly')
  await page.getByRole('button', { name: 'Generate now' }).click()

  const entry = page.getByRole('button', { name: new RegExp(`${start} → ${end}`) }).first()
  await expect(entry).toBeVisible({ timeout: 20_000 })
  await entry.click()
  await expect(page.getByText(/REPORT HISTORY · [1-9]/)).toBeVisible()
  await expect(page.getByRole('status').filter({ hasText: `Generated the weekly report for ${start} to ${end}.` })).toBeVisible()
})

test('the report is read as a document, says who generated it, and can be saved as a Markdown file', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  const week = freshWeek()
  await page.getByLabel('Period start').fill(week.start)
  await page.getByLabel('Period end').fill(week.end)
  await page.getByRole('button', { name: 'Generate now' }).click()

  const narrative = page.getByRole('region', { name: 'Report narrative' })
  // headings and lists, not the text of the Markdown with its # and - marks
  await expect(narrative.getByRole('heading', { name: `Investor Report: ${week.start} to ${week.end} (weekly)` })).toBeVisible()
  await expect(narrative.getByRole('heading', { name: 'Paper Trading Performance' })).toBeVisible()
  await expect(narrative.getByRole('listitem').filter({ hasText: 'Realized P&L this period' })).toBeVisible()
  await expect(narrative.getByText('## Paper Trading Performance').first()).toBeHidden() // only inside the closed "as written" disclosure
  await expect(narrative).toContainText('by user:e2e.pm@example.com')
  await expect(narrative).toContainText('India time')

  const [download] = await Promise.all([page.waitForEvent('download'), narrative.getByRole('button', { name: 'Download Markdown' }).click()])
  expect(download.suggestedFilename()).toBe(`investor-report-${week.start}-to-${week.end}-weekly.md`)
  const text = readFileSync((await download.path()) as string, 'utf-8')
  expect(text.startsWith(`# Investor Report: ${week.start} to ${week.end} (weekly)`)).toBe(true)
  expect(text).toContain('## Paper Trading Performance')
})

test('the form opens on the week just ended, follows the cadence until the dates are edited, and names a period the server would refuse', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  const today = todayInIndia(new Date())
  const week = defaultPeriod('weekly', today)
  const month = defaultPeriod('monthly', today)
  const start = page.getByLabel('Period start')
  const end = page.getByLabel('Period end')
  const generate = page.getByRole('button', { name: 'Generate now' })

  await expect(start).toHaveValue(week.start)
  await expect(end).toHaveValue(week.end)
  await expect(generate).toBeEnabled()

  await page.getByLabel('Cadence').selectOption('monthly')
  await expect(start).toHaveValue(month.start)
  await expect(end).toHaveValue(month.end)
  await expect(generate).toBeEnabled()

  // once the dates are edited, a change of cadence leaves them alone, and the form says what is now wrong
  await start.fill(addDays(month.start, -40))
  await page.getByLabel('Cadence').selectOption('weekly')
  await expect(start).toHaveValue(addDays(month.start, -40))
  await expect(page.getByRole('alert').filter({ hasText: /A weekly report covers at most 7 days/ })).toBeVisible()
  await expect(generate).toBeDisabled()

  await start.fill(week.start)
  await end.fill(addDays(week.start, -1))
  await expect(page.getByRole('alert').filter({ hasText: `The period ends (${addDays(week.start, -1)}) before it starts (${week.start}).` })).toBeVisible()
  await expect(generate).toBeDisabled()

  await end.fill(addDays(today, 3))
  await expect(page.getByRole('alert').filter({ hasText: 'nothing has happened on those days yet' })).toBeVisible()
  await expect(generate).toBeDisabled()
})

test('asking again for a period that has a report says so before it writes a second one', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  const week = freshWeek()
  await page.getByLabel('Period start').fill(week.start)
  await page.getByLabel('Period end').fill(week.end)
  await expect(page.getByText(/already exists/)).toHaveCount(0)

  await page.getByRole('button', { name: 'Generate now' }).click()
  await expect(page.getByRole('status').filter({ hasText: `Generated the weekly report for ${week.start} to ${week.end}.` })).toBeVisible()
  await expect(page.getByText(/A weekly report for this period already exists \(generated .* India time\)/)).toBeVisible()
})

test('the report open is in the address, so a reload opens the same one', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/investor-reports')
  const first = freshWeek()
  const second = { start: addDays(first.start, 7), end: addDays(first.end, 7) }
  const heading = (week: { start: string; end: string }) => page.getByRole('heading', { name: `Investor Report: ${week.start} to ${week.end} (weekly)` })

  await page.getByLabel('Period start').fill(first.start)
  await page.getByLabel('Period end').fill(first.end)
  await page.getByRole('button', { name: 'Generate now' }).click()
  await expect(heading(first)).toBeVisible()
  await expect(page).toHaveURL(/\/investor-reports\?report=[0-9a-f-]{36}$/)

  // the second report is the newest, so it is the one a bare address would open: the first, opened by
  // choosing it, and still open after a reload, is the proof that the address holds the choice
  await page.getByLabel('Period start').fill(second.start)
  await page.getByLabel('Period end').fill(second.end)
  await page.getByRole('button', { name: 'Generate now' }).click()
  await expect(heading(second)).toBeVisible()
  await page.getByRole('button', { name: new RegExp(`${first.start} → ${first.end}`) }).first().click()
  await expect(heading(first)).toBeVisible()
  await page.reload()
  await expect(heading(first)).toBeVisible()
})

test('a report that cannot be written gives the server\'s reason and keeps the form', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/investor-reports/generate', (route) => respond(route, { detail: 'the report writer is down' }, 500))
  await page.goto('/investor-reports')
  await page.getByRole('button', { name: 'Generate now' }).click()

  await expect(page.getByRole('alert').filter({ hasText: 'the report writer is down' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Generate now' })).toBeEnabled()
})

test('a read-only auditor can read reports but is not offered the form', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/investor-reports')

  await expect(page.getByRole('heading', { name: 'Investor reporting' })).toBeVisible()
  await expect(page.getByText(/REPORT HISTORY · \d+/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Generate now' })).toHaveCount(0)
})

for (const [who, user] of [
  ['a portfolio manager', E2E_USERS.pm],
  ['a read-only auditor', E2E_USERS.auditor],
] as const) {
  test(`a report list that cannot be loaded says so to ${who}, not "no reports yet"`, async ({ page }) => {
    await login(page, user)
    await page.route('**/api/v1/investor-reports', (route) =>
      route.request().method() === 'GET' ? respond(route, { detail: 'the report store is down' }, 500) : route.fallback(),
    )
    await page.goto('/investor-reports')

    const notice = page.getByRole('alert').filter({ hasText: 'Could not load the reports: the report store is down' })
    await expect(notice).toBeVisible()
    await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
    await expect(page.getByText(/No investor reports yet/)).toHaveCount(0)
  })
}

test('a report that cannot be opened says so, with a way to try again', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/investor-reports?report=00000000-0000-4000-8000-000000000000')

  const notice = page.getByRole('alert').filter({ hasText: 'Could not open that report: no such investor report' })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
})
