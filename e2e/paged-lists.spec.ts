import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, parseSeedOutput, runSeedScript, tokenFor } from './utils'

// The heavy lists are read a page at a time: Mission Control's runs, the strategies, and the
// instrument master behind Market Analysis. Each answers the rows of one page and how many there are
// in all. The rows are written directly (a hundred runs through the app would be a hundred pipeline
// runs): see backend/scripts/seed_e2e_runs.py and seed_e2e_instruments.py.

test.describe('runs', () => {
  // The finished runs written for these specs are dated two days back; they are removed afterwards, so
  // a spec that runs later sees only the runs it makes.
  const seededPrefixes: string[] = []
  test.afterAll(() => {
    for (const prefix of seededPrefixes) runSeedScript('seed_e2e_runs.py', ['cleanup', prefix])
  })

  test('Mission Control shows a page of runs with the total, and the older ones are on the pages after it', async ({ page }) => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_runs.py', ['120']))
    seededPrefixes.push(seeded.prefix)
    await login(page, E2E_USERS.pm)
    await page.goto('/mission-control')

    const pager = page.getByRole('navigation', { name: 'runs pages' })
    await expect(pager).toContainText(/Showing 1–50 of [\d,]+ runs/)
    // the seeded runs are older than any other, so their newest follows the others on the first page
    await expect(page.getByRole('button', { name: `Open run: ${seeded.newest}` })).toBeVisible()
    await expect(page.getByRole('button', { name: `Open run: ${seeded.oldest}` })).toHaveCount(0)

    await pager.getByRole('button', { name: 'Next page' }).click()
    await expect(pager).toContainText(/Showing 51–100 of [\d,]+ runs/)
    await expect(page.getByRole('button', { name: `Open run: ${seeded.newest}` })).toHaveCount(0)
    await expect(page.getByRole('button', { name: `Open run: ${seeded.prefix} #069` })).toBeVisible()

    await pager.getByRole('button', { name: 'Last page' }).click()
    await expect(page.getByRole('button', { name: `Open run: ${seeded.oldest}` })).toBeVisible()
  })

  test('a run waiting for a person stays on the board however many newer runs there are', async ({ page }) => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_runs.py', ['120']))
    const waiting = parseSeedOutput(runSeedScript('seed_e2e_runs.py', ['open']))
    seededPrefixes.push(seeded.prefix, waiting.objective)
    await login(page, E2E_USERS.pm)
    await page.goto('/mission-control')

    // three days old, so it is on the last page of the history (the board's pager), and still on the board
    await expect(page.getByRole('navigation', { name: 'runs pages' })).toContainText(/Showing 1–50 of [\d,]+ runs/)
    await expect(page.getByRole('button', { name: `Open run: ${waiting.objective}` })).toBeVisible()
    await expect(page.getByText('Open runs are always on the board; the pages are the history.')).toBeVisible()
  })

  test('the runs endpoint gives a page and its total, and the runs not yet finished on request', async () => {
    const token = await tokenFor(E2E_USERS.pm)
    const res = await fetch(`http://localhost:8000/api/v1/orchestration/runs?limit=5`, { headers: { Authorization: `Bearer ${token}` } })
    expect(res.status).toBe(200)
    expect(((await res.json()) as unknown[]).length).toBeLessThanOrEqual(5)
    expect(Number(res.headers.get('x-total-count'))).toBeGreaterThanOrEqual(0)

    const active = await api<{ status: string }[]>(token, '/api/v1/orchestration/runs?active=true&limit=200')
    expect(active.body.every((run) => ['pending', 'planning', 'running', 'paused'].includes(run.status))).toBe(true)
  })
})

test.describe('strategies', () => {
  test('the list gives a summary of each version, never its code, and a strategy read on its own still has it', async () => {
    // two versions, written directly (making one through the app would run the AI generator)
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_strategy_versions.py'))
    // the token comes after the seed script: a connection left idle while it ran is closed by the server
    const token = await tokenFor(E2E_USERS.admin)

    const listed = await fetch('http://localhost:8000/api/v1/strategies?limit=500', { headers: { Authorization: `Bearer ${token}` } })
    const body = (await listed.json()) as { id: string; versions: Record<string, unknown>[] }[]
    expect(Number(listed.headers.get('x-total-count'))).toBeGreaterThanOrEqual(body.length)
    const mine = body.find((s) => s.id === seeded.strategy_id)!
    expect(mine.versions).toHaveLength(2)
    for (const version of mine.versions) {
      expect(Object.keys(version).sort()).toEqual(['created_at', 'id', 'sandbox_passed', 'static_validation_passed', 'version_number'])
    }

    const whole = await api<{ versions: { code: string }[] }>(token, `/api/v1/strategies/${seeded.strategy_id}`)
    expect(whole.body.versions[0].code).toContain('def run_backtest')
  })

  test('the board still lists a strategy and opens it, now from the summary', async ({ page }) => {
    const { name } = parseSeedOutput(runSeedScript('seed_e2e_strategy_versions.py'))
    await login(page, E2E_USERS.auditor)
    await page.goto('/strategies')

    const card = page.getByRole('button', { name: new RegExp(name) })
    await expect(card).toBeVisible()
    await expect(card).toContainText('2 ver')
    await card.click()
    await expect(page).toHaveURL(/\/strategies\/[0-9a-f-]{36}/)
  })

  test('a strategy list that cannot be loaded says so to a role that cannot create one too', async ({ page }) => {
    await login(page, E2E_USERS.auditor)
    await page.route('**/api/v1/strategies?*', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        headers: { 'access-control-allow-origin': route.request().headers()['origin'] ?? '*', 'access-control-allow-credentials': 'true' },
        body: JSON.stringify({ detail: 'the strategy store is down' }),
      }),
    )
    await page.goto('/strategies')

    await expect(page.getByRole('alert').filter({ hasText: 'Could not load the strategies: the strategy store is down' })).toBeVisible()
    await page.unrouteAll({ behavior: 'ignoreErrors' })
  })
})

test.describe('the instrument master', () => {
  test('the symbol list is the first page of the master with the total, and what is typed narrows it on the server', async ({ page }) => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_instruments.py'))
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=technical')

    await expect(page.getByText(/Showing the first 50 of [\d,]+ symbols/)).toBeVisible()
    await expect(page.locator('.symbol-chip')).toHaveCount(50)

    const asked = page.waitForRequest((r) => r.url().includes('/market-data/instruments') && r.url().includes(`q=${seeded.equity_prefix}`))
    await page.getByLabel('Search symbol').fill(seeded.equity_prefix)
    await asked
    await expect(page.getByText(/Showing the first 50 of 130 symbols/)).toBeVisible()

    await page.getByLabel('Search symbol').fill(`${seeded.equity_prefix}007`)
    await expect(page.locator('.symbol-chip')).toHaveCount(1)
    await expect(page.getByText(/Showing the first/)).toHaveCount(0)
    await page.locator('.symbol-chip').click()
    await expect(page).toHaveURL(new RegExp(`symbol=${seeded.equity_prefix}007`))
  })

  test('a search is by any part of the symbol, in any case', async ({ page }) => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_instruments.py'))
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=freshness')

    await page.getByLabel('Search symbol').fill(`${seeded.equity_prefix.toLowerCase()}12`)

    // 120 to 129 (and the chip that opens what was typed, which is not a symbol in the master)
    await expect(page.locator('.symbol-chip').filter({ hasNotText: 'not in the instrument list' })).toHaveCount(10)
  })

  test('an underlying\'s option contracts are shown a page at a time, and the underlyings are listed by name', async ({ page }) => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_instruments.py'))
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=chain')

    await page.getByLabel('Underlying').selectOption(seeded.underlying)

    const pager = page.getByRole('navigation', { name: 'contracts pages' })
    await expect(pager).toContainText('Showing 1–100 of 120 contracts')
    await expect(page.locator('.option-chain-table').last().locator('tbody tr')).toHaveCount(100)
    await pager.getByRole('button', { name: 'Next page' }).click()
    await expect(pager).toContainText('Showing 101–120 of 120 contracts')
    await expect(page.locator('.option-chain-table').last().locator('tbody tr')).toHaveCount(20)
  })

  test('the underlyings with options are an answer of their own, not found by reading every contract', async () => {
    const seeded = parseSeedOutput(runSeedScript('seed_e2e_instruments.py'))
    const token = await tokenFor(E2E_USERS.pm)

    const underlyings = await api<string[]>(token, '/api/v1/market-data/option-underlyings')

    expect(underlyings.body).toContain(seeded.underlying)
    expect([...underlyings.body].sort()).toEqual(underlyings.body)
  })
})
