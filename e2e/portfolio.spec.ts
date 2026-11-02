import { expect, test, type Page, type Route } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Portfolio: the cross-broker rollup and the advisory recommendations. The rollup labels its P&L as
// before costs and shows the costs and the net beside it; only roles that may generate recommendations see
// the button; each read fails on its own; and a decision on a recommendation is confirmed before it is recorded.

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

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test('the rollup shows its figures, the P&L labelled as before costs, and the risk panel', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')

  await expect(page.getByRole('heading', { name: 'Portfolio', level: 1 })).toBeVisible()
  await expect(page.getByText('ACTIVE STRATEGIES')).toBeVisible()
  await expect(page.getByText('PAPER REALIZED P&L TODAY (BEFORE COSTS)')).toBeVisible()
  await expect(page.getByText(/costs [\d,.]+ · net -?[\d,.]+/)).toBeVisible()
  await expect(page.getByText(/As of \d{2} \w{3} \d{4}, \d{2}:\d{2}:\d{2} India time/)).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Risk metrics' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recommendations' })).toBeVisible()
})

test('a portfolio manager may generate a recommendation, an auditor may not', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto('/portfolio')
  await expect(page.getByRole('heading', { name: 'Portfolio', level: 1 })).toBeVisible()
  await expect(page.getByRole('button', { name: /Generate recommendation/ })).toHaveCount(0)

  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')
  await expect(page.getByRole('button', { name: /Generate recommendation/ })).toBeEnabled()
})

test('a rollup that cannot be loaded says so, with a way to try again, and the rest of the page still shows', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/portfolio/summary', (route) => respond(route, { detail: 'the ledger is down' }, 500))
  await page.goto('/portfolio')

  const notice = page.getByRole('alert').filter({ hasText: 'Could not load the portfolio summary: the ledger is down' })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  // a summary that failed does not take the risk metrics or the recommendations with it
  await expect(page.getByRole('heading', { name: 'Risk metrics' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recommendations' })).toBeVisible()
})

test('recommendations that cannot be loaded are not shown as "none yet"', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/portfolio/recommendations', (route) => respond(route, { detail: 'the store is down' }, 500))
  await page.goto('/portfolio')

  await expect(page.getByRole('alert').filter({ hasText: 'Could not load the recommendations: the store is down' })).toBeVisible()
  await expect(page.getByText('ACTIVE STRATEGIES')).toBeVisible()
  await expect(page.getByText(/No recommendations yet/)).toHaveCount(0)
})

test('risk metrics that cannot be loaded are named, and the summary and recommendations still show', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/portfolio/risk-metrics', (route) => respond(route, { detail: 'the positions are unreadable' }, 500))
  await page.goto('/portfolio')

  await expect(page.getByRole('alert').filter({ hasText: 'Could not load the risk metrics: the positions are unreadable' })).toBeVisible()
  await expect(page.getByText('ACTIVE STRATEGIES')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Recommendations' })).toBeVisible()
})

// Generates a recommendation and returns its card: the newest, so the first.
async function generateOne(page: Page) {
  await expect(page.getByText('ACTIVE STRATEGIES')).toBeVisible() // the page has loaded all three reads
  const cards = page.getByTestId('recommendation-card')
  const before = await cards.count()
  await page.getByRole('button', { name: /Generate recommendation/ }).click()
  await expect(cards).toHaveCount(before + 1)
  const card = cards.first()
  await expect(card).toHaveAttribute('data-status', 'pending')
  return card
}

test('accepting is confirmed first, says that nothing is traded, and can be cancelled', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')
  const card = await generateOne(page)

  await card.getByRole('button', { name: 'Accept' }).click()
  const dialog = page.getByRole('dialog', { name: 'Accept this recommendation?' })
  await expect(dialog).toContainText('Nothing is traded or rebalanced')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(card).toHaveAttribute('data-status', 'pending') // cancelling recorded nothing

  await card.getByRole('button', { name: 'Accept' }).click()
  await page.getByRole('dialog', { name: 'Accept this recommendation?' }).getByRole('button', { name: 'Accept', exact: true }).click()
  await expect(card).toHaveAttribute('data-status', 'accepted')
  await expect(card).toContainText('accepted by e2e.pm@example.com')
  await expect(card).toContainText('India time')
})

test('rejecting shows the note about to be recorded, and records it', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')
  const card = await generateOne(page)

  await card.getByLabel('Note for the decision').fill('too concentrated')
  await card.getByRole('button', { name: 'Reject' }).click()
  const dialog = page.getByRole('dialog', { name: 'Reject this recommendation?' })
  await expect(dialog).toContainText('cannot be reopened')
  await expect(dialog).toContainText('Your note: “too concentrated”')
  await dialog.getByRole('button', { name: 'Reject', exact: true }).click()

  await expect(card).toHaveAttribute('data-status', 'rejected')
  await expect(card).toContainText('rejected by e2e.pm@example.com')
  await expect(card).toContainText('"too concentrated"')
})

test('a decision the server refuses stays in the confirmation with its reason', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')
  const card = await generateOne(page)
  await page.route('**/api/v1/portfolio/recommendations/*/accept', (route) =>
    respond(route, { detail: 'Recommendation not found or already decided' }, 404),
  )

  await card.getByRole('button', { name: 'Accept' }).click()
  const dialog = page.getByRole('dialog', { name: 'Accept this recommendation?' })
  await dialog.getByRole('button', { name: 'Accept', exact: true }).click()

  await expect(dialog.getByRole('alert')).toContainText('Recommendation not found or already decided')
  await expect(dialog).toBeVisible()
})
