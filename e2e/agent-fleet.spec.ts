import { expect, test } from '@playwright/test'
import { E2E_USERS, api, configVersion, login, tokenFor } from './utils'

// Agent Fleet: the roster, the per-agent workspace, and who may edit an agent. Editing an identity
// writes the gateway config file, so a spec that changes one puts it back.

type Agent = { agent_id: string; display_name: string; emoji: string | null }

async function ceo(token: string): Promise<Agent> {
  const { body } = await api<Agent[]>(token, '/api/v1/agents')
  return body.find((agent) => agent.agent_id === 'ceo-agent')!
}

test('the roster loads, a search narrows it, and an agent opens in the workspace', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/agent-fleet')

  await expect(page.getByRole('heading', { name: 'Organization intelligence map' })).toBeVisible()
  await expect(page.getByText('ROSTER LOADED')).toBeVisible()

  await page.getByPlaceholder('Find an agent').fill('market analyst')
  await expect(page.getByRole('button', { name: /^Market Analyst/ })).toBeVisible()
  await expect(page.getByRole('button', { name: /^News Agent/ })).toHaveCount(0) // filtered out

  await page.getByRole('button', { name: /^Market Analyst/ }).click()
  await expect(page.getByRole('heading', { name: 'Market Analyst', level: 2 })).toBeVisible()
})

test('the workspace tabs are a tab list with a keyboard', async ({ page }) => {
  await login(page, E2E_USERS.pm)
  await page.goto('/agent-fleet')
  await expect(page.getByRole('tablist', { name: 'Agent workspace' })).toBeVisible()

  const identity = page.getByRole('tab', { name: 'Identity' })
  await expect(identity).toHaveAttribute('aria-selected', 'true')
  await identity.focus()
  await page.keyboard.press('ArrowRight')

  await expect(page.getByRole('tab', { name: 'Prompt Versions' })).toHaveAttribute('aria-selected', 'true')
  await expect(identity).toHaveAttribute('aria-selected', 'false')
})

test('only an administrator can edit an identity, and a cleared field stays cleared', async ({ page }) => {
  const adminToken = await tokenFor(E2E_USERS.admin)
  const before = await ceo(adminToken)

  // A portfolio manager sees the fields but cannot change them.
  await login(page, E2E_USERS.pm)
  await page.goto('/agent-fleet')
  await page.getByRole('button', { name: /^CEO Agent/ }).click()
  await expect(page.getByLabel('Emoji')).toBeDisabled()
  await expect(page.getByText('Identity fields are editable by a System Administrator only.')).toBeVisible()

  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/agent-fleet')
    await page.getByRole('button', { name: /^CEO Agent/ }).click()

    await page.getByLabel('Emoji').fill('🧪')
    await page.getByRole('button', { name: 'Save identity' }).click()
    await expect.poll(async () => (await ceo(adminToken)).emoji).toBe('🧪')

    // Clearing a field used to be ignored: the empty value was left out of the request.
    await page.getByLabel('Emoji').fill('')
    await page.getByRole('button', { name: 'Save identity' }).click()
    await expect.poll(async () => (await ceo(adminToken)).emoji || '').toBe('')
  } finally {
    await api(adminToken, '/api/v1/agents/ceo-agent/identity', {
      method: 'PUT',
      body: { name: before.display_name, emoji: before.emoji ?? '' },
      headers: (await configVersion(adminToken)).ifMatch,
    })
  }
})

test('a prompt version is read in full before it is activated', async ({ page }) => {
  const adminToken = await tokenFor(E2E_USERS.admin)
  const content = `E2E prompt draft ${Date.now()}: answer plainly and cite the source`
  const created = await api<{ id: string }>(adminToken, '/api/v1/agents/market-analyst/prompt-versions', {
    method: 'POST',
    body: { content },
  })
  expect(created.status).toBe(201)

  await login(page, E2E_USERS.admin)
  await page.goto('/agent-fleet')
  await page.getByRole('button', { name: /^Market Analyst/ }).click()
  await page.getByRole('tab', { name: 'Prompt Versions' }).click()

  await page.getByRole('button', { name: 'Review and activate' }).first().click()
  const dialog = page.getByRole('dialog', { name: /^Activate prompt v/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(content)).toBeVisible() // the prompt itself is on screen

  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toHaveCount(0)
  const { body: versions } = await api<{ id: string; status: string }[]>(adminToken, '/api/v1/agents/market-analyst/prompt-versions')
  expect(versions.find((version) => version.id === created.body.id)?.status).not.toBe('active') // cancelling activated nothing
})
