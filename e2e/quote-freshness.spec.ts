import { expect, test } from '@playwright/test'
import { E2E_USERS, api, login, parseSeedOutput, runSeedScript, tokenFor } from './utils'

// What a lane says about its latest price: when the exchange stamped it, which circuit band an order
// will be judged by, and a refused quote. The ticks are written by
// backend/scripts/seed_e2e_quote_freshness.py as the tick source would have left them.

async function openLane(page: import('@playwright/test').Page, symbol: string) {
  const lane = page.getByTestId(`lane-${symbol}`)
  await expect(lane).toBeVisible({ timeout: 15_000 })
  await lane.locator('.pt-lane-head').click()
  return lane
}

test('a lane dates its price by the exchange, and says which circuit band applies', async ({ page }) => {
  const seeded = parseSeedOutput(runSeedScript('seed_e2e_quote_freshness.py'))
  await login(page, E2E_USERS.pm)
  await page.goto('/paper-trading')

  const fresh = await openLane(page, seeded.fresh)
  await expect(fresh.getByTestId('price-freshness')).toContainText('Latest price ₹101.50')
  const stamp = fresh.getByTestId('exchange-freshness')
  await expect(stamp).toHaveAttribute('data-tone', 'fresh')
  await expect(stamp).toHaveText(/^Last traded at \d\d:\d\d:\d\d IST, \d+s ago\.$/)
  await expect(fresh.getByTestId('circuit-line')).toHaveAttribute('data-source', 'exchange')
  await expect(fresh.getByTestId('circuit-line')).toHaveText('Exchange circuit limits ₹90.00 to ₹110.00.')

  const quiet = await openLane(page, seeded.quiet)
  await expect(quiet.getByTestId('exchange-freshness')).toHaveAttribute('data-tone', 'quiet')
  await expect(quiet.getByTestId('exchange-freshness')).toContainText('A quiet instrument, or a feed that has stopped.')
  await expect(quiet.getByTestId('circuit-line')).toHaveAttribute('data-source', 'reference_table')
  await expect(quiet.getByTestId('circuit-line')).toContainText("the reference table's 20% band from the previous close (not an exchange figure)")

  const unstamped = await openLane(page, seeded.unstamped)
  await expect(unstamped.getByTestId('exchange-freshness')).toHaveAttribute('data-tone', 'unknown')
  await expect(unstamped.getByTestId('exchange-freshness')).toContainText('sends no exchange timestamp we can read')
})

test('a lane whose quotes are being refused says so and why', async ({ page }) => {
  const seeded = parseSeedOutput(runSeedScript('seed_e2e_quote_freshness.py'))
  await login(page, E2E_USERS.pm)
  await page.goto('/paper-trading')

  const refused = await openLane(page, seeded.refused)

  await expect(refused.getByTestId('quote-refusal')).toHaveText(
    'Its latest zerodha quote was refused: the exchange stamp (2026-10-30 15:29 IST) is from a previous day. Nothing trades on it until a current quote arrives.',
  )
  // The lane without a refusal has none.
  const fresh = await openLane(page, seeded.fresh)
  await expect(fresh.getByTestId('quote-refusal')).toHaveCount(0)
})

type ListedSubscription = {
  symbol: string
  last_tick: { lower_circuit: number | null; upper_circuit: number | null; exchange_age_seconds: number | null; exchange_time: string | null }
  quote_refusal: { broker: string } | null
}

test('the API serves the same facts the lane shows', async () => {
  const seeded = parseSeedOutput(runSeedScript('seed_e2e_quote_freshness.py'))
  const token = await tokenFor(E2E_USERS.auditor)

  const { body } = await api<ListedSubscription[]>(token, '/api/v1/paper-trading/subscriptions')

  const bySymbol = (symbol: string) => body.find((s) => s.symbol === symbol) as ListedSubscription
  const fresh = bySymbol(seeded.fresh)
  expect(fresh.last_tick.lower_circuit).toBe(90)
  expect(fresh.last_tick.upper_circuit).toBe(110)
  expect(fresh.last_tick.exchange_age_seconds).toBeGreaterThan(4)
  expect(fresh.quote_refusal).toBeNull()
  expect(bySymbol(seeded.refused).quote_refusal?.broker).toBe('zerodha')
  expect(bySymbol(seeded.unstamped).last_tick.exchange_time).toBeNull()
})

test('the provider status and the broker card report the quotes a broker had refused', async ({ page }) => {
  const token = await tokenFor(E2E_USERS.admin)
  const real = await api<Array<Record<string, unknown>>>(token, '/api/v1/broker-credentials')
  const stubbed = real.body.map((broker, index) =>
    index === 0
      ? {
          ...broker,
          configured: true,
          token_status: 'valid',
          quotes_refused_count: 3,
          quotes_refused_last_symbol: 'INFY',
          quotes_refused_last_reason: 'the exchange stamp (2026-10-30 15:29 IST) is from a previous day',
          quotes_refused_last_at: '2026-11-02T04:01:00Z',
        }
      : broker,
  )
  const name = String(stubbed[0].broker)
  const line =
    '3 quotes refused since the credentials last changed, the latest INFY at 09:31 IST: the exchange stamp (2026-10-30 15:29 IST) is from a previous day. Nothing trades on a refused quote.'

  await login(page, E2E_USERS.admin)
  await page.route('**/api/v1/broker-credentials', (route) => {
    if (route.request().method() !== 'GET') return route.continue()
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(stubbed) })
  })

  await page.goto('/analysis')
  await page.getByRole('tab', { name: 'Provider Status' }).click()
  await expect(page.getByTestId(`quote-refusals-${name}`)).toHaveText(line)

  await page.goto('/settings?section=broker')
  await expect(page.getByTestId(`quote-refusals-${name}`)).toContainText(line)
})
