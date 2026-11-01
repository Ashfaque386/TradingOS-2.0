import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Portfolio: the cross-broker rollup and the advisory recommendations. The rollup labels its P&L as
// before costs and shows the costs beside it; only roles that may generate recommendations see the button.

test('the rollup shows its figures, the P&L labelled as before costs, and the risk panel', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/portfolio')

  await expect(page.getByRole('heading', { name: 'Portfolio', level: 1 })).toBeVisible()
  await expect(page.getByText('ACTIVE STRATEGIES')).toBeVisible()
  await expect(page.getByText('PAPER REALIZED P&L TODAY (BEFORE COSTS)')).toBeVisible()
  await expect(page.getByText(/costs [\d,.]+ on top/)).toBeVisible()
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

test('a rollup that cannot be loaded says so, with a way to try again', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.route('**/api/v1/portfolio/summary', (route) =>
    route.fulfill({
      status: 500,
      contentType: 'application/json',
      headers: {
        'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
        // every console request carries credentials, and a browser refuses a faked answer without this
        'access-control-allow-credentials': 'true',
      },
      body: JSON.stringify({ detail: 'the ledger is down' }),
    }),
  )
  await page.goto('/portfolio')

  const notice = page.getByRole('alert').filter({ hasText: 'Could not load the portfolio: the ledger is down' })
  await expect(notice).toBeVisible()
  await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
})
