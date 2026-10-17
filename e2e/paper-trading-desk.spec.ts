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
    // Nothing prices the underlying here, so no "now" line is drawn or described.
    await expect(spread.locator('.pt-spot-line')).toHaveCount(0)
  })

  test('closing an unwatched position: says what it would do, and asks to be confirmed', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    await page.getByRole('alert').filter({ hasText: seeded.loose }).getByRole('button', { name: 'Close position' }).click()
    const dialog = page.getByRole('dialog')
    await expect(dialog).toContainText(`Close ${seeded.loose}`)

    // The estimate says why it cannot close yet (no real price was ever published) instead of
    // letting the operator find out by trying.
    const closeButton = dialog.getByRole('button', { name: 'Close position' })
    await expect(dialog.getByText(/no price has ever been published/)).toBeVisible()
    await expect(closeButton).toBeDisabled()

    // Even confirmed and explained, a normal close stays refused while there is nothing to close on.
    await dialog.getByLabel(`Type ${seeded.loose} to confirm`).fill(seeded.loose.toLowerCase())
    await dialog.getByLabel('Reason').fill('e2e cleanup')
    await expect(closeButton).toBeDisabled()
    await expect(dialog).toContainText('see the estimate')

    // A price the operator supplies is used as the reference, and shown as theirs. (Whether a
    // normal close can then fill depends on whether there is an order book to walk: this test
    // environment generates one, a real session needs the broker's, so that is not asserted.)
    await dialog.getByLabel(/Price/).fill('205')
    await expect(dialog.locator('.pt-estimate').getByText(/supplied/)).toBeVisible()

    // The administrator can clear it at a price they state, with a longer confirmation.
    await dialog.getByText('Administrative close').click()
    await expect(dialog.getByLabel(`Type ADMIN ${seeded.loose} to confirm`)).toBeVisible()
    await expect(closeButton).toBeDisabled() // the confirmation typed was for the other kind of close
    await dialog.getByLabel(`Type ADMIN ${seeded.loose} to confirm`).fill(`admin ${seeded.loose}`)
    await expect(dialog).toContainText('no costs')
    await closeButton.click()
    await expect(dialog).toBeHidden()
    await expect(page.getByRole('status').filter({ hasText: `Closed ${seeded.loose}` })).toBeVisible()
    await expect(page.getByRole('alert').filter({ hasText: seeded.loose })).toHaveCount(0)
  })

  test('adds a subscription: the symbol is checked as it is typed, and it can be added and switched off', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    await page.getByRole('button', { name: 'Add subscription' }).first().click()
    const dialog = page.getByRole('dialog', { name: 'Add a paper subscription' })
    await dialog.getByLabel('Strategy').selectOption({ label: seeded.strategy_name })
    await dialog.getByRole('combobox', { name: 'Symbol' }).fill('E2EADDED')

    // No broker is connected here: the search says so and the typed symbol is used as typed; the
    // preview says it cannot check the symbol or price it, and still lets it be added.
    await expect(dialog.getByRole('status')).toContainText(/no broker is connected/i)
    await expect(dialog.getByText("Can't be checked: no broker")).toBeVisible()
    await expect(dialog.getByRole('button', { name: 'Add subscription' })).toBeEnabled()

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

  test('changes a subscription\'s settings: shows what will change and when, and keeps capital fixed once it has traded', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/paper-trading')

    const lane = page.getByTestId(`lane-${seeded.held}`)
    await lane.getByRole('button', { name: new RegExp(seeded.held) }).click()
    await lane.getByRole('tab', { name: 'Settings' }).click()
    await expect(lane).toContainText('3%')
    await lane.getByRole('button', { name: 'Edit settings' }).click()

    const dialog = page.getByRole('dialog', { name: `Settings for ${seeded.held}` })
    const save = dialog.getByRole('button', { name: 'Save settings' })
    await expect(save).toBeDisabled()
    // It has traded (it holds shares and has paid costs), so its capital is not editable.
    await expect(dialog.getByLabel('Capital (₹)')).toHaveAttribute('readonly', '')

    // A value the backend would refuse is caught here, beside the field.
    await dialog.getByLabel('Size per entry (%)').fill('150')
    await expect(dialog.getByText('Use more than 0 and at most 100.')).toBeVisible()
    await expect(save).toBeDisabled()

    await dialog.getByLabel('Size per entry (%)').fill('5')
    await dialog.getByLabel('Stop-loss (%)').fill('6')
    const changes = dialog.getByRole('region', { name: 'What will change' })
    await expect(changes).toContainText('3% → 6%')
    await expect(changes).toContainText('open position from the next price tick')
    await expect(save).toBeDisabled() // a reason is still needed

    await dialog.getByLabel('Reason').fill('wider stop after review')
    await save.click()
    await expect(dialog).toBeHidden()
    await expect(page.getByRole('status').filter({ hasText: 'stop-loss 3% → 6%' })).toBeVisible()
    await expect(lane).toContainText('6%')
  })

  test('a risk manager sees the desk but has no controls', async ({ page }) => {
    await login(page, E2E_USERS.risk)
    await page.goto('/paper-trading')
    await expect(page.getByRole('heading', { name: 'Paper desk' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add subscription' })).toHaveCount(0)
    const lane = page.getByTestId(`lane-${seeded.held}`)
    await lane.getByRole('button', { name: new RegExp(seeded.held) }).click()
    await expect(page.getByRole('button', { name: 'Switch off' })).toHaveCount(0)
    await lane.getByRole('tab', { name: 'Settings' }).click()
    await expect(lane.getByText('Capital', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Edit settings' })).toHaveCount(0)
  })
})
