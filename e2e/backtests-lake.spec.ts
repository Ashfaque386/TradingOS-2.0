import { expect, test, type Page } from '@playwright/test'
import { E2E_USERS, api, login, parseSeedOutput, runSeedScript, tokenFor } from './utils'

// Backtests on the daily prices the data lake holds: a run made from a share and two dates, a
// walk-forward on it, a search for the best average length, the Monte Carlo choices, and what each refuses.
// backend/scripts/seed_e2e_lake.py writes 200 weekday bars of a new share each run (from a fixed first
// day, so the spec names the window). The E2E backend runs the synthetic demo mode, which reads the
// lake's bars whatever their source, so a run here says its prices are "Mixed", not "Real".

type Strategy = { id: string; name: string }

let lake: Record<string, string>
let strategyName: string

test.beforeAll(async () => {
  lake = parseSeedOutput(runSeedScript('seed_e2e_lake.py'))
  const token = await tokenFor(E2E_USERS.pm)
  const created = await api<Strategy>(token, '/api/v1/strategies', {
    method: 'POST',
    body: { name: `E2E lake backtests ${Date.now()}`, objective: 'E2E backtests from the lake' },
  })
  expect(created.status).toBe(201)
  strategyName = created.body.name
})

async function openNewBacktest(page: Page) {
  await page.goto('/backtests')
  await expect(page.getByRole('heading', { name: 'Backtests' })).toBeVisible()
  await page.getByRole('button', { name: 'New backtest' }).first().click()
  return page.getByRole('dialog', { name: 'New backtest' })
}

async function fillNewBacktest(dialog: ReturnType<Page['getByRole']>, over: { symbol?: string; start?: string; end?: string; average?: string } = {}) {
  await dialog.getByLabel('Strategy').selectOption({ label: strategyName })
  await dialog.getByLabel('Share').fill(over.symbol ?? lake.symbol)
  await dialog.getByLabel('First day').fill(over.start ?? lake.first)
  await dialog.getByLabel('Last day').fill(over.end ?? lake.last)
  if (over.average) await dialog.getByLabel('Average length (days)').fill(over.average)
}

async function runOnTheLake(page: Page) {
  await login(page, E2E_USERS.pm)
  const dialog = await openNewBacktest(page)
  await fillNewBacktest(dialog)
  await dialog.getByRole('button', { name: 'Run backtest' }).click()
  await expect(dialog).toHaveCount(0)
}

test('a backtest is started from a share and two dates, and shows what it rested on and how it was made', async ({ page }) => {
  await runOnTheLake(page)

  await expect(page.getByRole('status').filter({ hasText: new RegExp(`^Ran ${lake.symbol}: \\d+ trades\\.`) })).toBeVisible()
  const strip = page.getByLabel('This run')
  await expect(strip).toContainText(lake.symbol)
  await expect(strip).toContainText(`${lake.first} to ${lake.last}`)
  await expect(strip.locator('.source-chip')).toHaveText('Mixed') // the synthetic demo mode reads every bar
  await expect(strip).toContainText('Moving-average crossover, 15-day average · delivery · capital ₹1,00,000.00 · prices from the data lake')
  for (const metric of ['Total return', 'Sharpe', 'CAGR', 'Max Drawdown', 'Win Rate', 'Profit Factor', 'Trades']) {
    await expect(page.locator('.backtest-metric', { hasText: metric })).toBeVisible()
  }
})

test('the trade ledger shows each trade with its dates, and is paged', async ({ page }) => {
  await runOnTheLake(page)

  const ledger = page.getByRole('table', { name: 'Trades' })
  await expect(ledger.getByRole('columnheader', { name: 'Opened' })).toBeVisible()
  await expect(ledger.getByRole('columnheader', { name: 'Closed' })).toBeVisible()
  const first = ledger.locator('tbody tr').first()
  await expect(first.locator('td').nth(1)).toHaveText(/^\d{4}-\d{2}-\d{2}$/)
  await expect(first.locator('td').nth(2)).toHaveText(/^\d{4}-\d{2}-\d{2}$/)
  await expect(page.getByText(/^\d+ trades · \d+ won · \d+ lost · net /)).toBeVisible()
  await expect(page.getByRole('navigation', { name: 'trades pages' })).toContainText(/^Showing 1–\d+ of \d+ trades/)
})

test('a form with nothing filled in says what is missing and sends nothing', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  let sent = false
  await page.route('**/api/v1/backtests/from-lake', (route) => {
    sent = true
    return route.fallback()
  })
  const dialog = await openNewBacktest(page)

  await dialog.getByRole('button', { name: 'Run backtest' }).click()

  await expect(dialog.getByText('Pick the strategy this run belongs to.')).toBeVisible()
  await expect(dialog.getByText('Name the share the data lake holds prices for.')).toBeVisible()
  expect(sent).toBe(false)
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('a share the lake does not hold is refused with how many bars there are, and the dialog stays', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  const dialog = await openNewBacktest(page)
  await fillNewBacktest(dialog, { symbol: 'NOSUCHSHARE' })

  await dialog.getByRole('button', { name: 'Run backtest' }).click()

  await expect(dialog.getByRole('alert')).toContainText('The data lake holds 0 daily bars of NOSUCHSHARE')
  await expect(dialog).toBeVisible()
})

test('a window the lake does not reach up to its end is kept as a refused run, with its reason', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  const dialog = await openNewBacktest(page)
  // the lake ends on the last seeded bar; asked as of the end of last month it lacks the day before
  await fillNewBacktest(dialog, { end: '2026-10-30' })

  await dialog.getByRole('button', { name: 'Run backtest' }).click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: `${lake.symbol} was refused, so it has no result` })).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: `data lake lacks ${lake.symbol} data for 2026-10-29` })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run walk-forward' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Run Monte Carlo' })).toBeDisabled()
})

test('a walk-forward is run from the run itself, on the lake’s prices', async ({ page }) => {
  await runOnTheLake(page)

  await page.getByRole('button', { name: 'Run walk-forward' }).click()
  const dialog = page.getByRole('dialog', { name: `Walk-forward on ${lake.symbol}` })
  await expect(dialog).toContainText('Moving-average crossover, 15-day average')
  await expect(dialog.getByLabel('Training window (bars)')).toHaveValue('120')
  await dialog.getByRole('button', { name: 'Run walk-forward' }).click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByText(/^\d+ \/ \d+ windows passed$/)).toBeVisible()
  await expect(page.getByText(/^(Robust|Not robust)$/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Run walk-forward again' })).toBeVisible()
})

test('a walk-forward whose windows outrun the run is refused before the server is asked', async ({ page }) => {
  await runOnTheLake(page)
  await page.getByRole('button', { name: 'Run walk-forward' }).click()
  const dialog = page.getByRole('dialog', { name: `Walk-forward on ${lake.symbol}` })

  await dialog.getByLabel('Training window (bars)').fill('900')
  await dialog.getByRole('button', { name: 'Run walk-forward' }).click()

  await expect(dialog.getByText(/longer than this run/)).toBeVisible()
})

test('a search for the best average length reports the best of its tries and offers to run it', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/backtests')
  await page.getByRole('button', { name: 'Find best length' }).click()
  const dialog = page.getByRole('dialog', { name: 'Find the best average length' })

  await dialog.getByLabel('Strategy').selectOption({ label: strategyName })
  await dialog.getByLabel('Share').fill(lake.symbol)
  await dialog.getByLabel('First day').fill(lake.first)
  await dialog.getByLabel('Last day').fill(lake.last)
  await dialog.getByLabel('Shortest average (days)').fill('5')
  await dialog.getByLabel('Longest average (days)').fill('12')
  await dialog.getByLabel('Tries (1 to 200)').fill('6')
  await dialog.getByRole('button', { name: 'Search' }).click()

  const result = dialog.getByRole('status')
  await expect(result).toContainText(/^Best of 6 tries: a \d+-day average, sharpe ratio -?\d+\.\d\d\./)
  await expect(result).toContainText('so check it on a walk-forward before trusting it')

  await dialog.getByRole('button', { name: /^Run a backtest with \d+ days$/ }).click()
  const next = page.getByRole('dialog', { name: 'New backtest' })
  await expect(next.getByLabel('Share')).toHaveValue(lake.symbol)
  await expect(next.getByLabel('Average length (days)')).toHaveValue(/^([5-9]|1[0-2])$/)
})

test('a Monte Carlo run uses the number of paths chosen, and shows the worst path', async ({ page }) => {
  await runOnTheLake(page)

  await page.getByLabel('Monte Carlo paths').selectOption('5000')
  await page.getByRole('button', { name: 'Run Monte Carlo' }).click()

  const facts = page.getByLabel('Monte Carlo result')
  await expect(facts).toContainText('5,000', { timeout: 30_000 })
  await expect(facts).toContainText('95th percentile drawdown')
  await expect(facts).toContainText('Worst path drawdown')
})

test('a role that cannot start a run is not offered one, and reads the rest', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/backtests')
  await expect(page.getByRole('heading', { name: 'Backtests' })).toBeVisible()

  await expect(page.getByRole('button', { name: 'New backtest' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Find best length' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: /^Run walk-forward/ })).toHaveCount(0)
})
