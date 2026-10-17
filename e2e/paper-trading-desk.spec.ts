import { expect, test } from '@playwright/test'
import { E2E_USERS, login, parseSeedOutput, runSeedScript } from './utils'

// The paper-trading desk against the real backend and database. The seed script writes what the
// engine would have left behind (positions, fills with costs, a switched-off subscription that
// still holds shares, a credit spread); see backend/scripts/seed_e2e_paper_desk.py. There is no
// market data in this environment, so nothing is priced -- and the desk has to say so.

test.describe('paper-trading desk', () => {
  let seeded: Record<string, string>

  test.beforeAll(() => {
    seeded = parseSeedOutput(runSeedScript('seed_e2e_paper_desk.py'))
  })

  test('shows what is open, what it cost, and that nothing is priced', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')
    await expect(page.getByRole('heading', { name: 'Paper desk' })).toBeVisible()

    // The result is an equation: price result - costs = net.
    const ledger = page.getByRole('region', { name: "Today's result" })
    await expect(ledger).toContainText('Price result')
    await expect(ledger).toContainText('Net realized today')

    // A switched-off subscription that still holds shares is the first thing to see.
    const alert = page.getByRole('alert').filter({ hasText: seeded.loose })
    await expect(alert).toContainText('holding a position nobody is watching')

    // An open position with no price shows as unknown, never as zero.
    const held = page.getByTestId(`lane-${seeded.held}`)
    await held.getByRole('button', { name: new RegExp(seeded.held) }).click()
    await expect(held.getByText('never priced')).toBeVisible()
    await expect(held.getByText('unknown')).toBeVisible()

    // Where its costs went, from the fill that was charged.
    await held.getByRole('tab', { name: 'Costs and fills' }).click()
    await expect(held.getByText('Brokerage')).toBeVisible()
    await expect(held.getByText('STT')).toBeVisible()
  })

  test('a credit spread shows both legs and its capped payoff', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    const spread = page.getByTestId(`lane-${seeded.underlying}`)
    await spread.getByRole('button', { name: new RegExp(`${seeded.underlying} credit spread`) }).click()
    await expect(spread.getByText('Sold', { exact: true })).toBeVisible()
    await expect(spread.getByText('Bought', { exact: true })).toBeVisible()

    // Sold 24500 PE at 150, bought 24400 PE at 100, 50 units: +/-2,500, break-even 24450.
    await spread.getByRole('tab', { name: 'Payoff' }).click()
    await expect(spread.getByRole('img', { name: /Payoff at expiry/ })).toBeVisible()
    await expect(spread).toContainText('+₹2,500')
    await expect(spread).toContainText('−₹2,500')
    await expect(spread).toContainText('24450')
  })

  test('closing an unwatched position needs a reason, and with no real price the close refuses', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    await page.getByRole('alert').filter({ hasText: seeded.loose }).getByRole('button', { name: 'Close position' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toContainText(`Close ${seeded.loose}`)

    // No reason: refused here, before any request.
    await dialog.getByRole('button', { name: 'Close position' }).click()
    await expect(dialog.getByRole('alert')).toContainText('at least 3 characters')

    // With a reason but no real price anywhere, the engine refuses rather than inventing one.
    await dialog.getByLabel('Reason').fill('e2e cleanup')
    await dialog.getByRole('button', { name: 'Close position' }).click()
    await expect(dialog.getByRole('alert')).toBeVisible()
    await expect(dialog).toBeVisible()

    // The administrator can clear it at a price they state, and it is audited as administrative.
    await dialog.getByLabel(/Price/).fill('205')
    await dialog.getByText('Administrative close').click()
    await dialog.getByRole('button', { name: 'Close position' }).click()
    await expect(dialog).toBeHidden()
    await expect(page.getByRole('status').filter({ hasText: `Closed ${seeded.loose}` })).toBeVisible()
    await expect(page.getByRole('alert').filter({ hasText: seeded.loose })).toHaveCount(0)
  })

  test('adds a subscription and switches it off', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    await page.getByRole('button', { name: 'Add subscription' }).first().click()
    const dialog = page.getByRole('dialog', { name: 'Add a paper subscription' })
    await dialog.getByLabel('Strategy').selectOption({ label: seeded.strategy_name })
    await dialog.getByLabel('Symbol').fill('E2EADDED')
    await dialog.getByRole('button', { name: 'Add subscription' }).click()
    await expect(dialog).toBeHidden()

    const lane = page.getByTestId('lane-E2EADDED')
    await expect(lane).toBeVisible()
    await expect(lane.getByRole('tab', { name: 'Settings' })).toBeVisible() // opened on creation

    await lane.getByRole('button', { name: 'Switch off' }).click()
    const off = page.getByRole('dialog')
    await off.getByLabel('Reason').fill('e2e done')
    await off.getByRole('button', { name: 'Switch off' }).click()
    await expect(off).toBeHidden()
    await expect(lane.getByText('Off', { exact: true })).toBeVisible()

    await lane.getByRole('button', { name: 'Switch on' }).click()
    await expect(lane.getByText('Off', { exact: true })).toBeHidden()
  })

  test('a risk manager sees the desk but has no controls', async ({ page }) => {
    await login(page, E2E_USERS.risk)
    await page.goto('/paper-trading')
    await expect(page.getByRole('heading', { name: 'Paper desk' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add subscription' })).toHaveCount(0)
    await page.getByTestId(`lane-${seeded.held}`).getByRole('button', { name: new RegExp(seeded.held) }).click()
    await expect(page.getByRole('button', { name: 'Switch off' })).toHaveCount(0)
  })
})
