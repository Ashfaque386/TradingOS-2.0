import { expect, test, type Page } from '@playwright/test'
import { API_BASE_URL, E2E_USERS, api, login, parseSeedOutput, runSeedScript, tokenFor } from './utils'

// A strategy's own page and what is on it: an address of its own, every version and what changed between
// them, the suggestions made, and (for a live-eligible strategy) its live limits, which may only get
// stricter, and switching its subscription off. backend/scripts/seed_e2e_strategy_versions.py writes a
// strategy with two versions and two suggestions; seed_e2e_live_intent.py a live-eligible one (the
// same seed the live-autonomy spec enrols), and seed_e2e_live_position.py a position for it.

let versions: Record<string, string>

test.beforeAll(() => {
  versions = parseSeedOutput(runSeedScript('seed_e2e_strategy_versions.py'))
})

// ---- the strategy's own page ----

test('a strategy has an address of its own, a tab can be named in it, and it survives a reload', async ({ page }) => {
  await login(page, E2E_USERS.auditor)

  await page.goto(`/strategies/${versions.strategy_id}?tab=Versions`)

  await expect(page.getByRole('heading', { level: 1, name: versions.name })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Versions' })).toHaveAttribute('aria-selected', 'true')
  await page.reload()
  await expect(page.getByRole('heading', { level: 1, name: versions.name })).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Versions' })).toHaveAttribute('aria-selected', 'true')

  await page.getByRole('tab', { name: 'Overview' }).click()
  await expect(page).toHaveURL(new RegExp(`/strategies/${versions.strategy_id}$`))
})

test('opening a strategy from the board goes to its address, and back returns to the board', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/strategies')

  await page.getByRole('heading', { level: 3, name: versions.name, exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/strategies/${versions.strategy_id}$`))
  await expect(page.getByRole('heading', { level: 1, name: versions.name })).toBeVisible()

  await page.getByRole('button', { name: 'Back to lifecycle board' }).click()
  await expect(page).toHaveURL(/\/strategies$/)
})

test('a strategy that does not exist says so, with a way back', async ({ page }) => {
  await login(page, E2E_USERS.pm)

  await page.goto('/strategies/00000000-0000-4000-8000-000000000404')

  await expect(page.getByRole('alert').filter({ hasText: 'Could not load this strategy' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Back to lifecycle board' })).toBeVisible()
})

// ---- versions, what changed, suggestions ----

test('every version is listed newest first with where it came from', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto(`/strategies/${versions.strategy_id}?tab=Versions`)

  const table = page.getByRole('table', { name: 'Versions', exact: true })
  const rows = table.locator('tbody tr')
  await expect(rows).toHaveCount(2)
  await expect(rows.nth(0)).toContainText('v2')
  await expect(rows.nth(0)).toContainText('Regenerated from a suggestion by e2e.pm@example.com: “Tighten the stop and size the entry”')
  await expect(rows.nth(1)).toContainText('v1')
  await expect(rows.nth(1)).toContainText('Generated from the strategy’s objective')
  await expect(rows.nth(0)).toContainText('Passed static validation')
})

test('what changed between two versions is shown line by line, and is the same version against itself no change', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto(`/strategies/${versions.strategy_id}?tab=Versions`)

  // opens on the one before the newest and the newest
  await expect(page.getByLabel('Compare from')).toHaveValue(/.+/)
  await expect(page.getByLabel('Difference')).toHaveText('+3 −2 lines')
  const diff = page.getByRole('table', { name: 'Changes between the two versions' })
  await expect(diff.locator('tr[data-kind="add"]')).toHaveCount(3)
  await expect(diff.locator('tr[data-kind="remove"]')).toHaveCount(2)
  await expect(diff.locator('tr[data-kind="remove"]').first()).toContainText('stop = 5')
  await expect(diff.locator('tr[data-kind="add"]').first()).toContainText('stop = 3')

  const latest = await page.getByLabel('Compare to').inputValue()
  await page.getByLabel('Compare from').selectOption(latest)
  await expect(page.getByLabel('Difference')).toHaveText('No difference: the two are the same.')
})

test('the suggestions made are listed newest first, with what became of each', async ({ page }) => {
  await login(page, E2E_USERS.auditor)
  await page.goto(`/strategies/${versions.strategy_id}?tab=Versions`)

  const list = page.getByRole('list', { name: 'Suggestions' })
  const items = list.getByRole('listitem')
  await expect(items).toHaveCount(2)
  await expect(items.nth(0)).toContainText('Waiting for the AI review')
  await expect(items.nth(0)).toContainText('Skip the entry on a gap larger than two percent')
  await expect(items.nth(0)).toContainText('e2e.admin@example.com')
  await expect(items.nth(1)).toContainText('Turned into a new version')
  await expect(items.nth(1)).toContainText('→ became v2')
  await expect(items.nth(1)).toContainText('The reviewer: accept: A tighter stop is sensible.')
  await items.nth(1).getByText('What the regeneration changed').click()
  await expect(items.nth(1)).toContainText('+ size = 10')
})

test('the code of an older version can be read, and a suggestion is made on the version being read', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto(`/strategies/${versions.strategy_id}?tab=Versions`)

  await page.getByRole('button', { name: 'View the code of v1' }).click()

  await expect(page.getByRole('tab', { name: 'Code' })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByLabel('Version')).toHaveValue(/.+/)
  await expect(page.locator('pre.strategy-code')).toContainText('stop = 5')
  await expect(page.locator('pre.strategy-code')).not.toContainText('size = 10')
  await expect(page.getByText('generated_strategy.py · v1')).toBeVisible()
  await expect(page.getByText('Suggest Improvement to v1')).toBeVisible()
})

// ---- live limits ----

async function liveEligibleWithSubscription(): Promise<{ id: string; name: string; symbol: string; subscription: string }> {
  const seeded = parseSeedOutput(runSeedScript('seed_e2e_live_intent.py'))
  const token = await tokenFor(E2E_USERS.admin)
  const enrolled = await api<{ id: string }>(token, '/api/v1/live-trading/subscriptions', {
    method: 'POST',
    body: { strategy_id: seeded.strategy_id, symbol: seeded.symbol, broker_name: 'zerodha' },
  })
  expect(enrolled.status).toBe(201)
  return { id: seeded.strategy_id, name: seeded.name, symbol: seeded.symbol, subscription: enrolled.body.id }
}

async function openLifecycle(page: Page, strategyId: string) {
  await page.goto(`/strategies/${strategyId}?tab=Lifecycle`)
  await expect(page.getByLabel('Standing limits')).toBeVisible({ timeout: 20_000 })
}

test('the live limits are shown, and tightening one saves it, says so, and is audited with the reason', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  await login(page, E2E_USERS.admin)
  await openLifecycle(page, live.id)
  await expect(page.getByLabel('Standing limits')).toContainText('5 orders every 60 minutes')

  await page.getByRole('button', { name: 'Tighten limits' }).click()
  const dialog = page.getByRole('dialog', { name: `Tighten the limits on ${live.symbol}` })
  await dialog.getByLabel('Orders per window').fill('9')
  await expect(dialog.getByText(/can only be lowered here \(it is 5\)/)).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Tighten the limits' })).toBeDisabled()

  await dialog.getByLabel('Orders per window').fill('2')
  await expect(dialog.getByRole('status')).toContainText('Will change: Orders per window 5 → 2.')
  await dialog.getByLabel('Reason').fill('too many orders on the open')
  await dialog.getByRole('button', { name: 'Tighten the limits' }).click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: 'Limits tightened: Orders per window 5 → 2.' })).toBeVisible()
  await expect(page.getByLabel('Standing limits')).toContainText('2 orders every 60 minutes')

  const token = await tokenFor(E2E_USERS.admin)
  const audit = await api<{ details: { reason: string; changes: Record<string, { before: number; after: number }> } }[]>(
    token,
    `/api/v1/audit/entries?entity_id=${live.subscription}&action=live_trading_subscription.limits_tightened`,
  )
  expect(audit.body).toHaveLength(1)
  expect(audit.body[0].details.reason).toBe('too many orders on the open')
  expect(audit.body[0].details.changes.max_intents_per_window).toEqual({ before: 5, after: 2 })
})

test('a strategy holding shares cannot have its order cap cut below them or its subscription switched off', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  runSeedScript('seed_e2e_live_position.py', [live.id, live.symbol, '10', '100'])
  await login(page, E2E_USERS.admin)
  await openLifecycle(page, live.id)

  await page.getByRole('button', { name: 'Tighten limits' }).click()
  const dialog = page.getByRole('dialog', { name: `Tighten the limits on ${live.symbol}` })
  await expect(dialog).toContainText('holds 10 shares')
  await dialog.getByLabel('Largest order (₹)').fill('500')
  await expect(dialog.getByText(/would block the order that closes them/)).toBeVisible()
  await dialog.getByRole('button', { name: 'Cancel' }).click()

  await page.getByRole('button', { name: 'Switch off this subscription' }).click()
  const blocked = page.getByRole('dialog', { name: `${live.symbol} cannot be switched off` })
  await expect(blocked).toContainText('would be left unwatched')
  await expect(blocked).toContainText('Turn autonomous trading off instead')
  await blocked.getByRole('button', { name: 'Close' }).click()

  const token = await tokenFor(E2E_USERS.admin)
  const after = await fetch(`${API_BASE_URL}/api/v1/live-trading/subscriptions/${live.subscription}`, { headers: { Authorization: `Bearer ${token}` } })
  expect(((await after.json()) as { is_active: boolean }).is_active).toBe(true)
})

test('a subscription is switched off with its symbol typed and a reason, and the board then lists it as off', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  await login(page, E2E_USERS.admin)
  await openLifecycle(page, live.id)

  await page.getByRole('button', { name: 'Switch off this subscription' }).click()
  const dialog = page.getByRole('dialog', { name: `Switch off the ${live.symbol} subscription` })
  await expect(dialog).toContainText('cannot be undone here')
  const confirm = dialog.getByRole('button', { name: 'Switch it off' })
  await expect(confirm).toBeDisabled()
  await dialog.getByLabel('Reason').fill('retiring this symbol')
  await dialog.getByLabel(`Type ${live.symbol} to confirm`).fill(live.symbol)
  await confirm.click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByRole('status').filter({ hasText: `${live.symbol} is switched off.` })).toBeVisible()
  await expect(page.getByText('Switched off.', { exact: false }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: 'Tighten limits' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Enable autonomous trading' })).toHaveCount(0)

  await page.goto('/strategies')
  const row = page.getByRole('table', { name: 'Live subscriptions list' }).locator('tr', { hasText: live.name })
  await expect(row).toContainText('Switched off')
  await row.getByRole('link', { name: live.name }).click()
  await expect(page).toHaveURL(new RegExp(`/strategies/${live.id}\\?tab=Lifecycle$`))
})

test('the board lists every live subscription with whether it is trading on its own', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  await login(page, E2E_USERS.pm)
  await page.goto('/strategies')

  const section = page.getByRole('region', { name: 'Live subscriptions' })
  await expect(section).toBeVisible()
  await expect(section.getByRole('heading', { level: 2 })).toHaveText(/trading with real money on their own|Nothing is trading on its own/)
  const row = section.locator('tr', { hasText: live.name })
  await expect(row).toContainText('Autonomy off')
  await expect(row).toContainText('5 orders / 60 min · up to ₹50,000 an order')
})

test('the portfolio manager sees the limits but is not offered to change them', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  await login(page, E2E_USERS.pm)

  await openLifecycle(page, live.id)

  await expect(page.getByLabel('Standing limits')).toContainText('5 orders every 60 minutes')
  await expect(page.getByRole('button', { name: 'Tighten limits' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Switch off this subscription' })).toHaveCount(0)
})

test('the risk manager is offered to tighten the limits and to switch the subscription off', async ({ page }) => {
  const live = await liveEligibleWithSubscription()
  await login(page, E2E_USERS.risk)

  await openLifecycle(page, live.id)

  await expect(page.getByRole('button', { name: 'Tighten limits' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Switch off this subscription' })).toBeVisible()
})
