import { expect, test, type Route } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// Market Analysis against a real backend: the holiday calendar, the indicator periods, the data lake's
// lag, and the provider health. Answers that cannot be arranged in the database (a lake that is behind,
// a provider that failed) are stated to the page by a stubbed route.

function respond(route: Route, body: unknown, status = 200) {
  const origin = route.request().headers()['origin'] ?? '*'
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: { 'access-control-allow-origin': origin, 'access-control-allow-credentials': 'true' },
    body: JSON.stringify(body),
  })
}

let bars: Record<string, string>

test.beforeAll(() => {
  // 80 weekday bars ending yesterday: enough for the 26-bar MACD, too few for a 90-bar average.
  bars = parseSeedOutput(runSeedScript('seed_e2e_recent_bars.py'))
})

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test.describe('the view is in the address', () => {
  test('a tab opened from a link is the one shown, and a tab chosen is kept across a reload', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=holidays&year=2026')
    const tabs = page.getByRole('tablist', { name: 'Market analysis views' })
    await expect(tabs.getByRole('tab', { name: 'Holiday Calendar' })).toHaveAttribute('aria-selected', 'true')

    await tabs.getByRole('tab', { name: 'Datalake Status' }).click()
    await expect(page).toHaveURL(/\/analysis\?tab=datalake$/)
    await page.reload()
    await expect(tabs.getByRole('tab', { name: 'Datalake Status' })).toHaveAttribute('aria-selected', 'true')
  })

  test('an address that names a tab or a share the page does not know opens the Pulse, not an error', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=admin&symbol=%3Cscript%3E')
    await expect(page.getByRole('tab', { name: 'Pulse' })).toHaveAttribute('aria-selected', 'true')
  })
})

test.describe('the holiday calendar', () => {
  test('a year is listed with its weekday closures, and a holiday on a weekend is marked as changing nothing', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=holidays&year=2026')

    const table = page.getByRole('table', { name: 'Trading holidays in 2026' })
    const republicDay = table.getByRole('row', { name: /Republic Day/ })
    await expect(republicDay).toContainText('2026-01-26')
    await expect(republicDay).toContainText('Monday')
    await expect(republicDay).toContainText('Exchange shut')
    // 15 August 2026 is a Saturday
    const independenceDay = table.getByRole('row', { name: /Independence Day/ })
    await expect(independenceDay).toContainText('Saturday')
    await expect(independenceDay).toContainText('None: a weekend anyway')
    await expect(page.getByText(/\d+ weekday closures?/)).toBeVisible()
    // the calendar file lists 2026's movable holidays, so there is nothing to warn about
    await expect(page.getByRole('alert').filter({ hasText: 'movable holidays' })).toHaveCount(0)
  })

  test('a year the calendar file does not cover says its movable holidays are being treated as trading days', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=holidays&year=2040')

    const warning = page.getByRole('alert').filter({ hasText: 'lists no movable holidays for 2040' })
    await expect(warning).toBeVisible()
    await expect(warning).toContainText('treated as trading days')
    // the fixed-date holidays are still listed, and the year chosen is in the year list
    await expect(page.getByRole('table', { name: 'Trading holidays in 2040' }).getByRole('row', { name: /Republic Day/ })).toBeVisible()
    await expect(page.getByLabel('Year')).toHaveValue('2040')
  })

  test('choosing another year loads it and puts it in the address', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=holidays')
    const thisYear = Number(await page.getByLabel('Year').inputValue())
    await page.getByLabel('Year').selectOption(String(thisYear + 1))

    await expect(page.getByRole('table', { name: `Trading holidays in ${thisYear + 1}` })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`year=${thisYear + 1}`))
  })

  test('a calendar that cannot be loaded says so, with a way to try again', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/market-data/holidays*', (route) => respond(route, { detail: 'the calendar is down' }, 500))
    await page.goto('/analysis?tab=holidays')

    const notice = page.getByRole('alert').filter({ hasText: 'Could not load the holiday calendar: the calendar is down' })
    await expect(notice).toBeVisible()
    await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
})

test.describe('the indicator periods', () => {
  const open = async (page: import('@playwright/test').Page) => {
    await login(page, E2E_USERS.pm)
    await page.goto(`/analysis?tab=technical&symbol=${bars.symbol}`)
    await expect(page.getByRole('heading', { name: /SMA\(20\) · EMA\(20\) · Bollinger\(20, 2\)/ })).toBeVisible()
  }

  test('the charts are titled with the periods used, and the bars drawn are named', async ({ page }) => {
    await open(page)
    await expect(page.getByRole('heading', { name: 'RSI(14)' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'MACD(12, 26, 9)' })).toBeVisible()
    await expect(page.getByText(`80 BARS, ${bars.first} TO ${bars.last}`)).toBeVisible()
  })

  test('new periods are applied, asked of the server, and shown in every title', async ({ page }) => {
    await open(page)
    const form = page.getByRole('form', { name: 'Indicator settings' })
    await form.getByLabel('SMA window').fill('50')
    await form.getByLabel('RSI period').fill('7')
    await form.getByLabel('MACD fast').fill('5')
    await form.getByLabel('Bollinger deviations').fill('1.5')

    const asked = page.waitForRequest((r) => r.url().includes(`/indicators/${bars.symbol}`) && r.url().includes('sma_window=50'))
    await form.getByRole('button', { name: 'Apply' }).click()
    const url = (await asked).url()
    for (const part of ['rsi_period=7', 'macd_fast=5', 'bollinger_std=1.5', 'lookback_days=250']) expect(url).toContain(part)

    await expect(page.getByRole('heading', { name: /SMA\(50\) · EMA\(20\) · Bollinger\(20, 1.5\)/ })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'RSI(7)' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'MACD(5, 26, 9)' })).toBeVisible()
  })

  test('a period the server would refuse is refused in its box, and Apply waits', async ({ page }) => {
    await open(page)
    const form = page.getByRole('form', { name: 'Indicator settings' })

    await form.getByLabel('SMA window').fill('1')
    await expect(form.getByText('SMA window must be between 2 and 400.')).toBeVisible()
    await expect(form.getByRole('button', { name: 'Apply' })).toBeDisabled()

    await form.getByLabel('SMA window').fill('20')
    await form.getByLabel('MACD fast').fill('30')
    await expect(form.getByText("The MACD's fast average (30) must be shorter than its slow one (26).")).toBeVisible()
    await expect(form.getByRole('button', { name: 'Apply' })).toBeDisabled()

    await form.getByLabel('MACD fast').fill('12')
    await form.getByLabel('Bollinger deviations').fill('0')
    await expect(form.getByText('Bollinger deviations must be more than 0 and at most 5.')).toBeVisible()
  })

  test('an average longer than the history is named, not drawn as if it had numbers', async ({ page }) => {
    await open(page)
    const form = page.getByRole('form', { name: 'Indicator settings' })
    await form.getByLabel('SMA window').fill('90')
    await form.getByRole('button', { name: 'Apply' }).click()

    await expect(page.getByText(new RegExp(`SMA\\(90\\) needs 90 bars, and ${bars.symbol} has 80 in the last 250 days`))).toBeVisible()
    // and Reset puts the usual periods back
    await form.getByRole('button', { name: 'Reset to defaults' }).click()
    await expect(page.getByRole('heading', { name: /SMA\(20\) · EMA\(20\)/ })).toBeVisible()
    await expect(page.getByText(/needs 90 bars/)).toHaveCount(0)
  })

  test('a share that is not in the instrument list can be opened by typing it, and an empty lake is said to be empty', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=technical')
    await page.getByLabel('Search symbol').fill('zznotlisted')
    await page.getByRole('button', { name: 'Open ZZNOTLISTED (not in the instrument list)' }).click()

    await expect(page).toHaveURL(/symbol=ZZNOTLISTED/)
    await expect(page.getByText('No ingested OHLCV history for this symbol yet')).toBeVisible()
  })

  test('an instrument list that cannot be loaded says so, with a way to try again', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/market-data/instruments*', (route) => respond(route, { detail: 'the master is down' }, 500))
    await page.goto('/analysis?tab=technical')

    const notice = page.getByRole('alert').filter({ hasText: 'Could not load the instrument list: the master is down' })
    await expect(notice).toBeVisible()
    await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
})

test.describe('the data lake', () => {
  test('the day backtests require, and how far behind the newest daily data is, are shown', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/analysis?tab=datalake')

    await expect(page.getByText('Backtests require daily data for')).toBeVisible()
    await expect(page.getByTestId('lake-behind')).toContainText(/current|\d+ trading days? behind|no daily data in the lake/)
  })

  test('a lake that is behind is said to be, a failed run shows what failed, and times are India\'s', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/market-data/datalake/status', (route) =>
      respond(route, {
        as_of: '2026-11-02T04:00:00Z',
        pipelines: [
          {
            pipeline: 'incremental_daily',
            last_run_status: 'failed',
            last_run_at: '2026-11-02T01:00:00Z',
            last_run_source: 'zerodha',
            last_error: 'the broker refused the token',
            last_success_at: '2026-10-30T12:00:00Z',
          },
        ],
        symbols_with_daily_data: 3,
        symbols_with_intraday_data: 0,
        most_recent_daily_data_date: '2026-10-29',
        most_recent_intraday_data_date: null,
        required_daily_data_date: '2026-10-30',
        daily_data_behind_trading_days: 2,
      }),
    )
    await page.route('**/api/v1/market-data/provenance', (route) =>
      respond(route, [
        {
          id: 'run-1',
          pipeline: 'incremental_daily',
          source: 'zerodha',
          status: 'failed',
          symbols_processed: 0,
          rows_ingested: 0,
          error_message: 'the broker refused the token',
          details: null,
          started_at: '2026-11-02T01:00:00Z',
          completed_at: '2026-11-02T01:00:00Z',
        },
      ]),
    )
    await page.goto('/analysis?tab=datalake')

    await expect(page.getByTestId('lake-behind')).toContainText('2 trading days behind')
    await expect(page.getByText('2026-10-30', { exact: true })).toBeVisible()
    // 01:00 UTC is 06:30 in India
    await expect(page.getByText('02 Nov 2026, 06:30:00').first()).toBeVisible()
    await expect(page.getByText('02 Nov 2026, 09:30:00')).toBeVisible() // the status's own "as of", 04:00 UTC
    await expect(page.getByRole('row', { name: /Incremental Daily/ }).getByText('the broker refused the token')).toBeVisible()
  })

  test('a lake status that cannot be read says so, with a way to try again', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/market-data/datalake/status', (route) => respond(route, { detail: 'the lake is down' }, 500))
    await page.goto('/analysis?tab=datalake')

    const notice = page.getByRole('alert').filter({ hasText: 'Could not load the data lake status: the lake is down' })
    await expect(notice).toBeVisible()
    await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
})

test.describe('the provider health', () => {
  const providers = [
    { provider: 'anthropic', configured: true, base_url: null, in_fallback_order: true },
    { provider: 'openai', configured: true, base_url: null, in_fallback_order: true },
  ]

  test('each configured LLM provider says when it last answered and failed, and which one is active', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/settings/llm-providers', (route) => respond(route, providers))
    await page.route('**/api/v1/system/vitals', (route) =>
      respond(route, {
        llm: {
          active_provider: 'anthropic',
          token_usage_today: {},
          provider_health: [
            { provider: 'anthropic', last_success_at: '2026-11-02T04:00:00Z', last_failure_at: null, served_as_fallback: false },
            { provider: 'openai', last_success_at: '2026-11-01T04:00:00Z', last_failure_at: '2026-11-02T03:00:00Z', served_as_fallback: true },
          ],
        },
      }),
    )
    await page.goto('/analysis?tab=providers')

    await expect(page.getByTestId('llm-health-anthropic')).toHaveText('last answered 02 Nov 2026, 09:30:00')
    await expect(page.getByTestId('llm-health-openai')).toHaveText(
      'last answered 01 Nov 2026, 09:30:00 · last failed 02 Nov 2026, 08:30:00 · has served as the fallback',
    )
    await expect(page.getByText('ACTIVE', { exact: true })).toHaveCount(1)
  })

  test('a provider that has not answered since the backend started says so', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/settings/llm-providers', (route) => respond(route, providers))
    await page.route('**/api/v1/system/vitals', (route) =>
      respond(route, { llm: { active_provider: null, token_usage_today: {}, provider_health: [] } }),
    )
    await page.goto('/analysis?tab=providers')

    await expect(page.getByTestId('llm-health-anthropic')).toHaveText('has not answered since the backend started')
  })

  test('when the health cannot be read the credentials still show, and the failure is named', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/settings/llm-providers', (route) => respond(route, providers))
    await page.route('**/api/v1/system/vitals', (route) => respond(route, { detail: 'vitals are down' }, 500))
    await page.goto('/analysis?tab=providers')

    await expect(page.getByRole('alert').filter({ hasText: 'Could not load the provider health: vitals are down' })).toBeVisible()
    await expect(page.getByText('configured · in fallback chain').first()).toBeVisible()
    await expect(page.getByTestId('llm-health-anthropic')).toHaveCount(0)
  })
})

test('the pulse says when its figures are from, in India time', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/market-data/pulse', (route) =>
    respond(route, {
      as_of: '2026-11-02T04:00:00Z',
      india_vix: 14.2,
      sector_indices_change_pct: { NIFTY_BANK: 0.5 },
      global_indices_change_pct: { SP500: -0.2 },
    }),
  )
  await page.goto('/analysis')

  await expect(page.getByText('As of 02 Nov 2026, 09:30:00 India time, refreshed every 30 seconds')).toBeVisible()
})
