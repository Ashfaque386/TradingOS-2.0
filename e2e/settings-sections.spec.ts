import { expect, test, type Page, type Route } from '@playwright/test'
import { E2E_USERS, api, login, tokenFor } from './utils'

// Settings: the open section is kept in the address, and removing a saved key asks first. The keys and
// brokers themselves are stated to the page by stubbed routes, so no real credential is touched.

function respond(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'access-control-allow-origin': route.request().headers()['origin'] ?? '*',
      // every console request carries credentials, and a browser refuses a faked answer without this
      'access-control-allow-credentials': 'true',
    },
    body: status === 204 ? '' : JSON.stringify(body),
  })
}

const nav = (page: Page, label: RegExp) => page.getByRole('navigation', { name: 'Settings sections' }).getByRole('button', { name: label })

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test.describe('the section is in the address', () => {
  test('choosing a section puts it in the address, the first section is the bare page, and a reload opens the same one', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings')
    await expect(nav(page, /^Agent Gateway Config/)).toHaveAttribute('aria-current', 'page')

    await nav(page, /^Security/).click()
    await expect(page).toHaveURL(/\/settings\?section=security$/)
    await page.reload()
    await expect(nav(page, /^Security/)).toHaveAttribute('aria-current', 'page')
    await expect(page.getByRole('heading', { name: 'Sign-in', level: 2 })).toBeVisible()

    await nav(page, /^Agent Gateway Config/).click()
    await expect(page).toHaveURL(/\/settings$/)
  })

  test('a link to a section opens it, and a name the page does not know opens the first section instead', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=backups')
    await expect(nav(page, /^System and backups/)).toHaveAttribute('aria-current', 'page')

    await page.goto('/settings?section=nonsense')
    await expect(nav(page, /^Agent Gateway Config/)).toHaveAttribute('aria-current', 'page')
    await expect(page).toHaveURL(/\/settings$/)
  })

  test('the status strip\'s chips open their section and put it in the address', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings')
    const chip = page.locator('.set-status-chip').first()
    await expect(chip).toBeVisible()
    await chip.click()

    await expect(page).toHaveURL(/\/settings\?section=(broker|llm|notifications)$/)
  })
})

test.describe('removing a saved key asks first', () => {
  test('a broker\'s credentials: the dialog names the broker, cancelling deletes nothing, confirming deletes them', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const real = await api<Array<Record<string, unknown>>>(token, '/api/v1/broker-credentials')
    const configured = real.body.map((broker, index) => (index === 0 ? { ...broker, configured: true, token_status: 'valid' } : broker))
    const name = String(configured[0].broker)
    let deletes = 0

    await login(page, E2E_USERS.admin)
    await page.route('**/api/v1/broker-credentials', (route) => (route.request().method() === 'GET' ? respond(route, configured) : route.fallback()))
    await page.route(`**/api/v1/broker-credentials/${name}`, (route) => {
      if (route.request().method() !== 'DELETE') return route.fallback()
      deletes += 1
      return respond(route, null, 204)
    })
    await page.goto('/settings?section=broker')

    await page.getByRole('button', { name: 'Remove', exact: true }).first().click()
    const dialog = page.getByRole('dialog', { name: `Remove the ${name} credentials?` })
    await expect(dialog).toContainText('cannot be read back')
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await expect(dialog).toHaveCount(0)
    expect(deletes).toBe(0)

    await page.getByRole('button', { name: 'Remove', exact: true }).first().click()
    await page.getByRole('dialog', { name: `Remove the ${name} credentials?` }).getByRole('button', { name: 'Remove', exact: true }).click()
    await expect.poll(() => deletes).toBe(1)
  })

  test('an LLM provider\'s key: the dialog says it leaves the fallback chain, and cancelling changes nothing', async ({ page }) => {
    let writes = 0
    await login(page, E2E_USERS.admin)
    await page.route('**/api/v1/settings/llm-providers', (route) =>
      route.request().method() === 'GET'
        ? respond(route, [{ provider: 'anthropic', configured: true, base_url: null, in_fallback_order: true }])
        : route.fallback(),
    )
    await page.route(/\/api\/v1\/(settings\/llm-providers\/anthropic|gateway\/llm-provider-order)$/, (route) => {
      if (route.request().method() !== 'GET') writes += 1
      return route.fallback()
    })
    await page.goto('/settings?section=llm')

    await page.getByRole('button', { name: 'Remove', exact: true }).first().click()
    const dialog = page.getByRole('dialog', { name: 'Remove the Anthropic key?' })
    await expect(dialog).toContainText('taken out of the fallback chain')
    await dialog.getByRole('button', { name: 'Cancel' }).click()

    await expect(dialog).toHaveCount(0)
    expect(writes).toBe(0)
  })
})
