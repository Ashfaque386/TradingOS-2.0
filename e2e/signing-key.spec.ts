import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// Settings > Security: the key that signs every login token. The card says
// where the key came from and whether it can be trusted -- never the key itself
// -- and rotates it. A deployment started with defaults used to be seeded with
// the public placeholder `change-me-in-production`, which made every token
// forgeable; the backend now refuses to seed that, and this card is how an
// administrator replaces a weak key that is already stored.
//
// The rotation test signs everyone out, by design. Every other spec logs in
// afresh, so it can sit mid-suite.

const card = (page: import('@playwright/test').Page) => page.getByLabel('JWT signing key')

test('an admin sees the key is healthy, and the key itself is never on the page', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=security')

  await expect(card(page).getByText('Key looks strong')).toBeVisible()
  // The suite's backend is started with this value as its environment key.
  await expect(page.locator('body')).not.toContainText('dev-only-secret-do-not-use-in-production')
})

test('a role that is not an administrator sees the section but no controls', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/settings?section=security')

  await expect(card(page).getByText('SystemAdministrator role required')).toBeVisible()
  await expect(card(page).getByRole('button', { name: 'Rotate signing key' })).toHaveCount(0)
})

test('rotating needs a confirm step, signs everyone out, and a new login works', async ({ page }) => {
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=security')

  // Backing out changes nothing.
  await card(page).getByRole('button', { name: 'Rotate signing key' }).click()
  await expect(card(page).getByRole('group', { name: 'Confirm rotation' })).toContainText('everyone')
  await card(page).getByRole('button', { name: 'Cancel' }).click()
  await expect(card(page).getByRole('button', { name: 'Rotate signing key' })).toBeVisible()

  await card(page).getByRole('button', { name: 'Rotate signing key' }).click()
  await card(page).getByRole('button', { name: 'Rotate now' }).click()
  await expect(card(page).getByRole('status')).toContainText('Rotated.')

  await card(page).getByRole('button', { name: 'Sign in again' }).click()
  await expect(page).toHaveURL(/\/login/)

  // The old session is gone; a fresh login works, and the key is now the one
  // this administrator created.
  await login(page, E2E_USERS.admin)
  await page.goto('/settings?section=security')
  await expect(card(page).getByText('Key looks strong')).toBeVisible()
  await expect(card(page)).toContainText(E2E_USERS.admin.email)
})
