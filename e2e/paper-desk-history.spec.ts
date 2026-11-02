import { readFileSync } from 'node:fs'
import { expect, test, type Page, type Route } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// The Paper Desk's history: fetching a subscription's price history, its daily signals, every order it
// has made (opened eight at a time) and the fills as a file, and a desk that stays up when a figure
// cannot be read. backend/scripts/seed_e2e_paper_history.py writes a subscription with 12 orders (one a
// manual close) and two signals (one acted on); its symbol is new each run, and it is switched off so
// that the engine, which trades every active subscription while the market is open, leaves it alone.
// With the synthetic feed a subscription's price history is generated, so "waiting for price history"
// cannot happen here: the tests that need it state it in the list the page reads (as other specs state
// a failing server), and what they check is what the console does with it.

let seed: Record<string, string>

test.beforeAll(() => {
  seed = parseSeedOutput(runSeedScript('seed_e2e_paper_history.py'))
})

// The desk polls, so a stated list can still be mid-request when a test ends; let it finish quietly.
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

const lane = (page: Page) => page.getByTestId(`lane-${seed.symbol}`)

async function openLane(page: Page, tab?: string) {
  await page.goto('/paper-trading')
  await expect(page.getByRole('heading', { name: 'Paper desk' })).toBeVisible()
  await lane(page).getByRole('button', { name: new RegExp(seed.symbol) }).click()
  if (tab) await lane(page).getByRole('tab', { name: tab }).click()
}

/** The desk's list of subscriptions, with ours stated as switched on and waiting for price history. */
async function stateAwaitingHistory(page: Page) {
  await page.route('**/api/v1/paper-trading/subscriptions', async (route) => {
    if (route.request().method() !== 'GET') return route.fallback()
    const response = await route.fetch()
    const rows = (await response.json()) as Record<string, unknown>[]
    const patched = rows.map((row) =>
      row.symbol === seed.symbol
        ? { ...row, is_active: true, state: 'awaiting_history', state_reason: 'It needs more real price history before it can signal.' }
        : row,
    )
    await route.fulfill({ response, json: patched })
  })
}

async function answerBackfill(page: Page, body: Record<string, unknown>) {
  await page.route('**/api/v1/paper-trading/subscriptions/*/backfill-history', (route) => answer(route, 200, body))
}

function answer(route: Route, status: number, body: unknown) {
  const origin = route.request().headers()['origin'] ?? '*'
  const cors = {
    'access-control-allow-origin': origin,
    'access-control-allow-credentials': 'true',
    'access-control-allow-headers': route.request().headers()['access-control-request-headers'] ?? 'authorization',
    'access-control-allow-methods': 'GET, POST, OPTIONS',
  }
  if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
  return route.fulfill({ status, contentType: 'application/json', headers: cors, body: JSON.stringify(body) })
}

test('a subscription waiting for price history is offered the fetch, and a refusal is said and changes nothing', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await stateAwaitingHistory(page)
  await answerBackfill(page, {
    status: 'failed',
    symbol: seed.symbol,
    rows: 0,
    first_date: null,
    last_date: null,
    source: null,
    unreal_rows_removed: 0,
    error: 'no broker is connected',
  })
  await page.goto('/paper-trading')

  const waiting = page.getByRole('status').filter({ hasText: `${seed.symbol} is waiting for price history` })
  await expect(waiting).toBeVisible()
  await waiting.getByRole('button', { name: 'Fetch price history' }).click()

  const dialog = page.getByRole('dialog', { name: `Fetch price history for ${seed.symbol}` })
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('replaces any generated or untagged rows')
  await dialog.getByRole('button', { name: 'Fetch history' }).click()

  // the broker's reason is shown and the dialog stays for another try
  await expect(dialog.getByRole('alert')).toHaveText(`Could not fetch the price history of ${seed.symbol}: no broker is connected.`)
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(waiting).toBeVisible()
})

test('a fetch that works says what it got and what it replaced', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await stateAwaitingHistory(page)
  await answerBackfill(page, {
    status: 'success',
    symbol: seed.symbol,
    rows: 1240,
    first_date: '2021-11-02',
    last_date: '2026-10-30',
    source: 'zerodha',
    unreal_rows_removed: 12,
    error: null,
  })
  await page.goto('/paper-trading')

  await page.getByRole('status').filter({ hasText: `${seed.symbol} is waiting for price history` }).getByRole('button', { name: 'Fetch price history' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Fetch history' }).click()

  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: `Fetched 1,240 daily prices for ${seed.symbol} from zerodha (2021-11-02 to 2026-10-30). 12 generated rows were replaced.` })).toBeVisible()
})

test('the lane offers the fetch too, to an operator and to no one else', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await stateAwaitingHistory(page)
  await openLane(page)
  await expect(lane(page).getByRole('button', { name: 'Fetch price history' })).toBeVisible()

  await login(page, E2E_USERS.risk)
  await openLane(page)
  await expect(lane(page).getByRole('button', { name: 'Fetch price history' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Fetch price history' })).toHaveCount(0)
})

test('the signals tab lists them newest first, and says which a position was taken on', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await openLane(page, 'Signals')

  const rows = lane(page).getByRole('table', { name: 'Daily signals' }).locator('tbody tr')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toContainText('Sell')
  await expect(rows.nth(0)).toContainText('Not acted on yet')
  await expect(rows.nth(0)).toContainText('₹111.00')
  await expect(rows.nth(1)).toContainText('Buy')
  await expect(rows.nth(1)).toContainText(/Acted on 02 Nov 2026, \d\d:\d\d:\d\d/)
})

test('orders open eight at a time, newest first, and a manual close is marked', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await openLane(page, 'Costs and fills')

  const orders = lane(page).locator('.pt-order')
  await expect(lane(page).getByText('ORDERS · 12')).toBeVisible()
  await expect(orders).toHaveCount(8)
  await expect(lane(page).getByRole('button', { name: 'Show 4 more (4 older)' })).toBeVisible()
  // the manual close is the sixth-oldest of the twelve, so it is among the newest eight
  await expect(lane(page).getByText('manual close')).toHaveCount(1)
  // newest first: the newest order was a sell (index 11), at the highest price
  await expect(orders.first()).toContainText('Sold 10')
  await expect(orders.first()).toContainText('₹111.00')
})

test('showing more reaches every order', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await openLane(page, 'Costs and fills')
  const orders = lane(page).locator('.pt-order')
  await expect(orders).toHaveCount(8)

  await lane(page).getByRole('button', { name: 'Show 4 more (4 older)' }).click()

  await expect(orders).toHaveCount(12)
  await expect(lane(page).getByRole('button', { name: /^Show/ })).toHaveCount(0)
})

test('Show all opens the whole list at once', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await openLane(page, 'Costs and fills')

  await lane(page).getByRole('button', { name: 'Show all 12' }).click()

  await expect(lane(page).locator('.pt-order')).toHaveCount(12)
})

test('the fills can be saved as a file with one row for each, oldest first, and the manual close marked', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await openLane(page, 'Costs and fills')

  const [download] = await Promise.all([page.waitForEvent('download'), lane(page).getByRole('button', { name: /Export all fills/ }).click()])

  expect(download.suggestedFilename()).toMatch(new RegExp(`^paper-fills-${seed.symbol}-\\d{4}-\\d{2}-\\d{2}\\.csv$`))
  const lines = readFileSync((await download.path()) as string, 'utf-8').trimEnd().split('\r\n')
  expect(lines).toHaveLength(13) // the header and 12 fills
  expect(lines[0]).toMatch(/^created_at_utc,time_india,symbol,side,/)
  const prices = lines.slice(1).map((line) => Number(line.split(',')[7])) // the India-time cell holds a comma
  expect(prices).toEqual([...prices].sort((a, b) => a - b)) // seeded so that price rises with time: oldest first
  // the manual_close column is followed by the order's id and the leg index; fully_filled, also true, is not
  expect(lines.filter((line) => /,true,[0-9a-f-]{36},0$/.test(line))).toHaveLength(1)
})

test('a figure that cannot be read leaves the desk standing, and says so', async ({ page }) => {
  await login(page, E2E_USERS.risk)
  await page.route('**/api/v1/paper-trading/pnl/today', (route) => answer(route, 500, { detail: 'the fills store is down' }))

  await page.goto('/paper-trading')

  await expect(page.getByRole('alert').filter({ hasText: 'Today’s result could not be read (the fills store is down)' })).toBeVisible()
  // the subscriptions, which are the page, are still there
  await expect(lane(page)).toBeVisible()
  await expect(page.getByRole('region', { name: "Today's result" })).toBeVisible()
})
