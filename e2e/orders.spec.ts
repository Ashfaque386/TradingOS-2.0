import { expect, test } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// The Orders & Trades page against a real backend: the ledger paged and filtered by the server, where each
// price came from, a strategy's live orders with the broker's id or the reason one failed, and the intents.
// backend/scripts/seed_e2e_orders.py writes a strategy with 30 paper fills (one partial, one real, one a
// manual close), two live orders (one accepted, one failed with a reason), open positions (one on a
// switched-off subscription) and three live intents; every name in it is new each run.

let seed: Record<string, string>

test.beforeAll(() => {
  seed = parseSeedOutput(runSeedScript('seed_e2e_orders.py'))
})

async function openLedgerFor(page: import('@playwright/test').Page, user: { email: string; password: string } = E2E_USERS.pm) {
  await login(page, user)
  await page.goto('/orders')
  await expect(page.getByRole('heading', { name: 'Orders & Trades' })).toBeVisible()
  await page.getByLabel('Strategy', { exact: true }).selectOption({ label: seed.name })
}

test('the ledger is paged by the server across a strategy’s executions, to the last', async ({ page }) => {
  await openLedgerFor(page)

  // 30 paper fills and 2 live trades
  await expect(page.getByText('Showing 1–25 of 32 executions')).toBeVisible()
  await expect(page.locator('.orders-table tbody tr')).toHaveCount(25)

  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByText('Showing 26–32 of 32 executions')).toBeVisible()
  await expect(page.locator('.orders-table tbody tr')).toHaveCount(7)
  await expect(page.getByRole('button', { name: 'Next page' })).toBeDisabled()
  await expect(page.getByText(/Not refreshing on this page/)).toBeVisible()

  await page.getByLabel('Rows per page').selectOption('50')
  await expect(page.getByText('Showing 1–32 of 32 executions')).toBeVisible()
})

test('filters are applied by the server and say how many match', async ({ page }) => {
  await openLedgerFor(page)
  await expect(page.getByText('Showing 1–25 of 32 executions')).toBeVisible()

  await page.getByRole('button', { name: 'PAPER', exact: true }).click()
  await expect(page.getByText('Showing 1–25 of 30 executions')).toBeVisible()

  await page.getByLabel('Status', { exact: true }).selectOption('partial')
  await expect(page.getByText('Showing 1–1 of 1 executions')).toBeVisible()
  await expect(page.locator('.orders-table tbody tr')).toHaveCount(1)

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await page.getByLabel('Strategy', { exact: true }).selectOption({ label: seed.name })
  await page.getByRole('button', { name: 'LIVE', exact: true }).click()
  await page.getByLabel('Status', { exact: true }).selectOption('failed')
  await expect(page.getByText('Showing 1–1 of 1 executions')).toBeVisible()

  await page.getByLabel('Status', { exact: true }).selectOption('rejected')
  await expect(page.getByText('No executions match these filters.')).toBeVisible()

  await page.getByRole('button', { name: 'Clear filters' }).click()
  await page.getByLabel('Strategy', { exact: true }).selectOption({ label: seed.name })
  await page.getByLabel('Search symbol').fill(seed.symbol.toLowerCase().slice(0, 6))
  await expect(page.getByText('Showing 1–25 of 30 executions')).toBeVisible()
})

test('each row says where its price came from and what it cost', async ({ page }) => {
  await openLedgerFor(page)
  await page.getByRole('button', { name: 'PAPER', exact: true }).click()
  await page.getByLabel('Rows per page').selectOption('50')
  await expect(page.getByText('Showing 1–30 of 30 executions')).toBeVisible()

  await expect(page.locator('.source-chip', { hasText: 'Simulated' }).first()).toBeVisible()
  await expect(page.locator('.source-chip', { hasText: 'Real' })).toHaveCount(1)
  await expect(page.getByText('manual close')).toHaveCount(1)
  await expect(page.getByText('₹12.50')).toHaveCount(1) // the one fill that cost something
})

test('an auditor is offered the audit trail of a live trade, and only of a live one', async ({ page }) => {
  await openLedgerFor(page, E2E_USERS.auditor)

  // the two live trades (one accepted, one failed) are the newest rows, so they are on the first page
  await expect(page.getByRole('link', { name: /^Audit trail of buy/ })).toHaveCount(2)
  await expect(page.locator(`a[href="/audit?entity_id=${seed.live_trade_id}"]`)).toHaveCount(1)
  await page.getByRole('button', { name: 'PAPER', exact: true }).click()
  await expect(page.getByText('Showing 1–25 of 30 executions')).toBeVisible()
  await expect(page.getByRole('link', { name: /^Audit trail of/ })).toHaveCount(0)
})

test('a role that cannot read the audit log is not offered the trail', async ({ page }) => {
  await openLedgerFor(page, E2E_USERS.pm)

  await expect(page.getByRole('columnheader', { name: 'AUDIT' })).toHaveCount(0)
  await expect(page.getByRole('link', { name: /^Audit trail of/ })).toHaveCount(0)
})

test('a strategy’s live orders show the broker’s id, or why one failed, and open into their trades', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/orders')
  await page.getByRole('tab', { name: /Live by strategy/ }).click()
  await expect(page.getByText('Pick a strategy to see its positions')).toBeVisible()

  await page.getByLabel('Strategy to inspect').selectOption({ label: seed.name })

  const orders = page.getByRole('table', { name: 'Live orders' })
  await expect(orders.getByText(seed.broker_order_id)).toBeVisible()
  await expect(page.getByRole('alert').filter({ hasText: seed.failure_reason })).toBeVisible()
  // newest first: the failed order (2h after the base time) is above the accepted one (1h)
  const brokerIds = await orders.locator('tbody tr td:nth-child(7)').allInnerTexts()
  expect(brokerIds.map((s) => s.trim()).filter((s) => s !== '')).toEqual(['—', seed.broker_order_id])

  await page.getByRole('button', { name: new RegExp(`^Show trades of order ${seed.broker_order_id}`) }).click()
  const trades = page.getByRole('table', { name: /^Trades of order/ })
  await expect(trades).toBeVisible()
  await expect(trades.getByText('₹1,490.00')).toBeVisible()
  await expect(trades.getByText('Real')).toBeVisible()
})

test('positions say they are before costs, and one nothing is watching says so', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/orders')

  const side = page.locator('.orders-side')
  await expect(side.getByText('-₹300.50').first()).toBeVisible()
  await expect(side.getByText('before costs').first()).toBeVisible()
  await expect(side.getByText(/Not watched: its subscription is switched off/).first()).toBeVisible()
})

test('the intents are filtered by the server and counted', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/orders')
  await page.getByRole('tab', { name: /Live Order Intents/ }).click()

  await page.getByLabel('Intent strategy').selectOption({ label: seed.name })
  await expect(page.getByText('Showing 1–3 of 3 intents')).toBeVisible()

  await page.getByLabel('Intent status').selectOption('capped')
  await expect(page.getByText('Showing 1–1 of 1 intents')).toBeVisible()
  await expect(page.getByText('CAPPED', { exact: true })).toBeVisible()
})

test('the header counts come from the server, so they are whole numbers of the whole ledger', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/orders')

  const total = page.locator('.summary-stat', { hasText: 'TOTAL EXECUTIONS' })
  await expect(total.locator('strong')).toHaveText(/^[1-9][\d,]*$/)
  await expect(total.locator('small')).toHaveText(/paper · [\d,]+ live/)
})
