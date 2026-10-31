import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// Backtests: runs are created through the API (the console does not collect bars yet), so each spec
// seeds its own strategy and runs and reads them back in the page.

type Strategy = { id: string; name: string; versions: Array<{ id: string }> }
type Run = { id: string; symbol: string; status: string }

function bars(days: number, startDay: number) {
  // Weekday-free simple series: dates need only be unique, ascending ISO days.
  const out = []
  let close = 100
  for (let i = 0; i < days; i += 1) {
    close *= 1 + (((i * 7919) % 13) - 6) / 600
    const date = new Date(Date.UTC(2024, 0, startDay + i)).toISOString().slice(0, 10)
    out.push({ date, open: close, high: close * 1.01, low: close * 0.99, close, volume: 25_000 })
  }
  return out
}

async function seedStrategyWithRun(token: string, label: string, symbol: string, series = bars(80, 1)): Promise<{ strategy: Strategy; run: Run }> {
  const created = await api<Strategy>(token, '/api/v1/strategies', {
    method: 'POST',
    body: { name: `E2E backtests ${label} ${Date.now()}`, objective: 'E2E backtest seed' },
  })
  expect(created.status).toBe(201)
  const ran = await api<Run>(token, '/api/v1/backtests', {
    method: 'POST',
    body: {
      strategy_version_id: created.body.versions[0].id,
      symbol,
      bars: series,
      strategy: 'always_long',
      as_of: series[series.length - 1].date,
    },
  })
  expect(ran.status).toBe(201)
  return { strategy: created.body, run: ran.body }
}

test('a seeded run shows its metrics, and the strategy filter keeps only that strategy’s runs', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  const first = await seedStrategyWithRun(token, 'one', 'E2EBTONE')
  await seedStrategyWithRun(token, 'two', 'E2EBTTWO')

  await login(page, E2E_USERS.pm)
  await page.goto('/backtests')
  await expect(page.getByRole('heading', { name: 'Backtests' })).toBeVisible()

  const runSelect = page.locator('select.backtest-select').nth(1)
  await expect(runSelect.locator('option', { hasText: 'E2EBTONE' })).toHaveCount(1)
  await expect(runSelect.locator('option', { hasText: 'E2EBTTWO' })).toHaveCount(1)

  // Filtering by a strategy used to match only its newest version; now it matches all of them.
  await page.locator('select.backtest-select').first().selectOption(first.strategy.id)
  await expect(runSelect.locator('option', { hasText: 'E2EBTONE' })).toHaveCount(1)
  await expect(runSelect.locator('option', { hasText: 'E2EBTTWO' })).toHaveCount(0)

  await runSelect.selectOption(first.run.id)
  for (const metric of ['Total return', 'Sharpe', 'CAGR', 'Max Drawdown', 'Win Rate', 'Profit Factor', 'Trades']) {
    await expect(page.locator('.backtest-metric', { hasText: metric })).toBeVisible()
  }
})

test('comparing two runs with under ten shared days says n/a instead of crashing', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.pm)
  // Two runs whose dates never overlap: there is no honest correlation, and the server answers null.
  await seedStrategyWithRun(token, 'early', 'E2EBTEARLY', bars(40, 1))
  await seedStrategyWithRun(token, 'late', 'E2EBTLATE', bars(40, 200))

  await login(page, E2E_USERS.pm)
  await page.goto('/backtests')
  await page.locator('.run-chip', { hasText: 'E2EBTEARLY' }).click()
  await page.locator('.run-chip', { hasText: 'E2EBTLATE' }).click()
  await page.getByRole('button', { name: /^Compare selected \(2\)$/ }).click()

  await expect(page.getByText('n/a · under 10 shared days')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Backtests' })).toBeVisible() // the page is still there
})

test('only roles that may run Monte Carlo get an enabled button, and a run saves its result', async ({ page }) => {
  const pmToken = await tokenFor(E2E_USERS.pm)
  const { run } = await seedStrategyWithRun(pmToken, 'mc', 'E2EBTMC')

  await login(page, E2E_USERS.auditor)
  await page.goto('/backtests')
  await expect(page.getByRole('button', { name: 'Run Monte Carlo' })).toBeDisabled()

  await login(page, E2E_USERS.pm)
  await page.goto('/backtests')
  await page.locator('select.backtest-select').nth(1).selectOption(run.id)
  await page.getByRole('button', { name: 'Run Monte Carlo' }).click()

  await expect(page.getByText('95th percentile drawdown')).toBeVisible({ timeout: 20_000 })
})
