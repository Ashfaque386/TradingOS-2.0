import { expect, test, type Page } from '@playwright/test'
import { E2E_USERS, api, configVersion, login, tokenFor } from './utils'

// Edits of the agent gateway config, made in Agent Fleet and in Settings, against a real backend: each
// is made on the version the page read, so one that lost a race says so and saves nothing; a save
// leaves one version, not two; skills are chosen from the shipped set; and an agent can be narrated.
// The races are arranged by answering the page's own request: the other editor's change is made at the
// moment the page's is sent, on the version both read, so there is no timing to hope for.

type Agent = { agent_id: string; enabled: boolean; skills: string[]; heartbeat_enabled: boolean; config_version_id: number }

const ADMIN = E2E_USERS.admin.email

async function agentsOf(token: string): Promise<Agent[]> {
  return (await api<Agent[]>(token, '/api/v1/agents')).body
}

/** The newest version's number. Versions are numbered in order, so one more than before is one new
 * version (the list itself stops at 100). */
async function newestVersion(token: string): Promise<number> {
  return (await api<{ id: number }[]>(token, '/api/v1/gateway/config/versions')).body[0].id
}

/** Puts an agent back as it was: its switch, heartbeat and skills. */
async function restore(token: string, before: Agent[], ids: string[]) {
  for (const id of ids) {
    const was = before.find((a) => a.agent_id === id)!
    const now = (await agentsOf(token)).find((a) => a.agent_id === id)!
    const patch: Record<string, unknown> = {}
    if (now.enabled !== was.enabled) patch.enabled = was.enabled
    if (now.heartbeat_enabled !== was.heartbeat_enabled) patch.heartbeat_enabled = was.heartbeat_enabled
    if (JSON.stringify([...now.skills].sort()) !== JSON.stringify([...was.skills].sort())) patch.skills = was.skills
    if (Object.keys(patch).length === 0) continue
    await api(token, `/api/v1/agents/${id}`, { method: 'PATCH', body: patch, headers: (await configVersion(token)).ifMatch })
  }
}

/** When the page sends a request matching `url`, another editor's change is made first, on the version
 * the page's own request names, so the page's request arrives after the config has moved on. */
async function otherEditorBeats(page: Page, token: string, url: string | RegExp, change: { agent: string; body: Record<string, unknown> }) {
  let beaten = false
  await page.route(url, async (route) => {
    if (route.request().method() === 'GET' || beaten) return route.fallback()
    beaten = true
    const ifMatch = route.request().headers()['if-match']
    const made = await api(token, `/api/v1/agents/${change.agent}`, { method: 'PATCH', body: change.body, headers: { 'If-Match': ifMatch } })
    expect(made.status).toBe(200)
    return route.fallback()
  })
}

test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: 'ignoreErrors' })
})

test.describe('an edit that lost a race', () => {
  test('in Agent Fleet it saves nothing, says so, and the page shows what the other editor did', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/agent-fleet')
      await page.getByRole('button', { name: /^Market Analyst/ }).click()
      await page.getByRole('tab', { name: 'Enable / Disable' }).click()
      await otherEditorBeats(page, token, '**/api/v1/agents/market-analyst', { agent: 'news-agent', body: { enabled: false } })

      await page.getByRole('button', { name: 'Toggle agent status' }).click()

      await expect(page.getByRole('alert').filter({ hasText: 'nothing was saved' })).toBeVisible()
      // the page reloaded: the other editor's change is there, and this one is not
      await expect(page.getByRole('button', { name: /^News Agent/ }).getByRole('img', { name: 'Status: disabled' })).toBeVisible()
      const after = await agentsOf(token)
      expect(after.find((a) => a.agent_id === 'market-analyst')!.enabled).toBe(true)
      expect(after.find((a) => a.agent_id === 'news-agent')!.enabled).toBe(false)
    } finally {
      await restore(token, before, ['market-analyst', 'news-agent'])
    }
  })

  test('in Settings, a change of the LLM fallback order says so and keeps the order that is there', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    const orderOf = async () =>
      (await api<{ parsed: { infra: { llmProviders: { order: string[] } } } }>(token, '/api/v1/gateway/config')).body.parsed.infra.llmProviders.order
    const original = await orderOf()
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/settings?section=llm')
      await expect(page.locator('.set-llm-priority-row')).toHaveCount(original.length)
      await otherEditorBeats(page, token, '**/api/v1/gateway/llm-provider-order', { agent: 'news-agent', body: { enabled: false } })

      await page.locator('.set-llm-priority-row').nth(1).getByRole('button', { name: /^Move .* up$/ }).click()

      await expect(page.getByText('nothing was saved')).toBeVisible()
      expect(await orderOf()).toEqual(original)
    } finally {
      await restore(token, before, ['news-agent'])
    }
  })

  test('in the raw editor, a save on a stale version is refused and the editor shows the current text', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/settings')
      await page.getByRole('tab', { name: 'Raw JSON' }).click()
      const editor = page.locator('textarea.set-raw-editor')
      await expect(editor).not.toHaveValue('')
      // another editor changes an agent while this one has the file open
      const { ifMatch } = await configVersion(token)
      await api(token, '/api/v1/agents/news-agent', { method: 'PATCH', body: { enabled: false }, headers: ifMatch })

      await page.getByRole('button', { name: 'Save & apply' }).click()

      await expect(page.getByText('nothing was saved')).toBeVisible()
      await expect(editor).toHaveValue(/"news-agent"/) // reloaded: it now holds the other editor's change
    } finally {
      await restore(token, before, ['news-agent'])
    }
  })
})

test.describe('saving', () => {
  test('a change made in Agent Fleet leaves one new version, and the watcher does not add a second', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/agent-fleet')
      await page.getByRole('button', { name: /^Market Analyst/ }).click()
      await page.getByRole('tab', { name: 'Heartbeat' }).click()
      const versions = await newestVersion(token)

      await page.getByRole('button', { name: 'Toggle heartbeat' }).click()

      await expect(page.getByRole('status').filter({ hasText: 'Saved: heartbeat.' })).toBeVisible()
      expect(await newestVersion(token)).toBe(versions + 1)
      // the watcher notices the file the save wrote a moment later, and used to apply it again
      await page.waitForTimeout(4_000)
      expect(await newestVersion(token)).toBe(versions + 1)
    } finally {
      await restore(token, before, ['market-analyst'])
    }
  })

  test('the audit trail names the person who made the change', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/agent-fleet')
      await page.getByRole('button', { name: /^Market Analyst/ }).click()
      await page.getByRole('tab', { name: 'Heartbeat' }).click()
      await page.getByRole('button', { name: 'Toggle heartbeat' }).click()
      await expect(page.getByRole('status').filter({ hasText: 'Saved: heartbeat.' })).toBeVisible()

      // the version this change made has an audit entry, and it names the administrator, not "cli"
      const version = await newestVersion(token)
      const audit = await api<{ actor: string }[]>(
        token,
        `/api/v1/audit/entries?action=agent_gateway.config_applied&entity_type=agent_config_version&entity_id=${version}`,
      )
      expect(audit.status).toBe(200)
      expect(audit.body.map((e) => e.actor)).toEqual([ADMIN])
    } finally {
      await restore(token, before, ['market-analyst'])
    }
  })

  test('the LLM fallback order is reordered with the keyboard-reachable buttons, as one change', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const orderOf = async () =>
      (await api<{ parsed: { infra: { llmProviders: { order: string[] } } } }>(token, '/api/v1/gateway/config')).body.parsed.infra.llmProviders.order
    const original = await orderOf()
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/settings?section=llm')
      await expect(page.locator('.set-llm-priority-row')).toHaveCount(original.length)
      const versions = await newestVersion(token)

      await page.locator('.set-llm-priority-row').nth(2).getByRole('button', { name: /^Move .* up$/ }).click()

      const swapped = [...original]
      ;[swapped[1], swapped[2]] = [swapped[2], swapped[1]]
      await expect.poll(orderOf).toEqual(swapped)
      expect(await newestVersion(token)).toBe(versions + 1)
    } finally {
      const { ifMatch } = await configVersion(token)
      await api(token, '/api/v1/gateway/llm-provider-order', { method: 'PUT', body: { order: original }, headers: ifMatch })
    }
  })

  test('text the app cannot run on is not applied, and the running configuration and the file are unchanged', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const { id } = await configVersion(token)
    await login(page, E2E_USERS.admin)
    await page.goto('/settings')
    await page.getByRole('tab', { name: 'Raw JSON' }).click()
    await page.locator('textarea.set-raw-editor').fill('{ version: 1, infra: ')

    await page.getByRole('button', { name: 'Save & apply' }).click()

    await expect(page.getByText('That text was not applied')).toBeVisible()
    expect((await configVersion(token)).id).toBe(id)
  })

  test('a rollback asks first, and cancelling applies nothing', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      // a version older than the one running, so there is something to roll back to
      await api(token, '/api/v1/agents/market-analyst', {
        method: 'PATCH',
        body: { heartbeat_enabled: !before.find((a) => a.agent_id === 'market-analyst')!.heartbeat_enabled },
        headers: (await configVersion(token)).ifMatch,
      })
      const { id } = await configVersion(token)
      await login(page, E2E_USERS.admin)
      await page.goto('/settings')

      await page.getByRole('button', { name: /Rollback/ }).first().click()
      const dialog = page.getByRole('dialog', { name: /^Roll back to v\d+\?$/ })
      await expect(dialog).toContainText('applied again as a new version')
      await dialog.getByRole('button', { name: 'Cancel' }).click()

      await expect(dialog).toHaveCount(0)
      expect((await configVersion(token)).id).toBe(id)
    } finally {
      await restore(token, before, ['market-analyst'])
    }
  })
})

test.describe('skills', () => {
  test('a skill is chosen from the shipped set, described before it is granted, and revoked', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/agent-fleet')
      await page.getByRole('button', { name: /^Market Analyst/ }).click()
      await page.getByRole('tab', { name: 'Skills' }).click()

      await expect(page.getByPlaceholder('skill-id')).toHaveCount(0) // there is no box to type an id into
      const choose = page.getByLabel('Skill to grant')
      const options = await choose.locator('option').allInnerTexts()
      expect(options).toContain('code-format-lint')
      expect(options).not.toContain('market-data-read') // held already
      expect(options.find((o) => o.startsWith('option-chain-read'))).toContain('(placeholder)')

      await choose.selectOption('code-format-lint')
      await expect(page.getByText('Checks that Python code parses')).toBeVisible()
      await page.getByRole('button', { name: 'Grant skill' }).click()

      await expect(page.getByRole('status').filter({ hasText: 'Saved: skills.' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Revoke code-format-lint' })).toBeVisible()
      await page.getByRole('button', { name: 'Revoke code-format-lint' }).click()
      await expect(page.getByRole('button', { name: 'Revoke code-format-lint' })).toHaveCount(0)
    } finally {
      await restore(token, before, ['market-analyst'])
    }
  })

  test('someone who may not edit sees the skills but no way to grant or revoke one', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/agent-fleet')
    await page.getByRole('button', { name: /^Market Analyst/ }).click()
    await page.getByRole('tab', { name: 'Skills' }).click()

    await expect(page.getByText(/GRANTED SKILLS · \d+/)).toBeVisible()
    await expect(page.getByLabel('Skill to grant')).toHaveCount(0)
    await expect(page.getByRole('button', { name: /^Revoke / })).toHaveCount(0)
  })

  test('the Skill Marketplace lists every shipped skill, marks the placeholders, and has no box for a made-up id', async ({ page }) => {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=skills')

    for (const skill of ['code-format-lint', 'market-data-read', 'notification-send', 'option-chain-read', 'portfolio-status-read', 'sandbox-dry-run']) {
      await expect(page.locator('.set-skill-card', { hasText: skill })).toHaveCount(1)
    }
    await expect(page.locator('.set-skill-card', { hasText: 'option-chain-read' })).toContainText('placeholder')
    await expect(page.locator('.set-skill-card', { hasText: 'notification-send' })).toContainText('sends messages')
    await expect(page.locator('.set-skill-card', { hasText: 'code-format-lint' })).not.toContainText('placeholder')
    await expect(page.getByPlaceholder('new-skill-id')).toHaveCount(0)
  })

  test('a grant is made from the marketplace\'s matrix and says what it did', async ({ page }) => {
    const token = await tokenFor(E2E_USERS.admin)
    const before = await agentsOf(token)
    try {
      await login(page, E2E_USERS.admin)
      await page.goto('/settings?section=skills')
      await page.locator('.set-skill-card', { hasText: 'code-format-lint' }).click()
      const dialog = page.getByRole('dialog', { name: 'Grants of code-format-lint' })

      await dialog.getByRole('button', { name: 'Grant code-format-lint for Market Analyst' }).click()

      await expect(dialog.getByRole('button', { name: 'Revoke code-format-lint for Market Analyst' })).toBeVisible()
      expect((await agentsOf(token)).find((a) => a.agent_id === 'market-analyst')!.skills).toContain('code-format-lint')
    } finally {
      await restore(token, before, ['market-analyst'])
    }
  })
})

test.describe('the narration', () => {
  test('says in sentences what an agent is doing, grouped, with when it was written in India time', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.goto('/agent-fleet')
    await page.getByRole('button', { name: /^Market Analyst/ }).click()
    await page.getByRole('tab', { name: 'Narration' }).click()

    await expect(page.getByText('Market Analyst is switched on.')).toBeVisible()
    await expect(page.getByText(/It may use \d+ skills?: /)).toBeVisible()
    await expect(page.getByText(/India time/)).toBeVisible()
    for (const heading of ['SETTINGS', 'HEARTBEAT', 'WORK']) await expect(page.getByText(heading, { exact: true })).toBeVisible()
  })

  test('an agent with no data source says it reports nothing rather than a made-up figure', async ({ page }) => {
    await login(page, E2E_USERS.auditor)
    await page.goto('/agent-fleet')
    await page.getByRole('button', { name: /^Macro/ }).click()
    await page.getByRole('tab', { name: 'Narration' }).click()

    await expect(page.getByText('WHAT IS MISSING', { exact: true })).toBeVisible()
    await expect(page.getByText(/reports nothing rather than a made-up figure/)).toBeVisible()
  })

  test('a narration that cannot be loaded says so, with a way to try again', async ({ page }) => {
    await login(page, E2E_USERS.pm)
    await page.route('**/api/v1/agents/market-analyst/narration', (route) =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        headers: { 'access-control-allow-origin': route.request().headers()['origin'] ?? '*', 'access-control-allow-credentials': 'true' },
        body: JSON.stringify({ detail: 'the recorder is down' }),
      }),
    )
    await page.goto('/agent-fleet')
    await page.getByRole('button', { name: /^Market Analyst/ }).click()
    await page.getByRole('tab', { name: 'Narration' }).click()

    const notice = page.getByRole('alert').filter({ hasText: 'Could not load the narration: the recorder is down' })
    await expect(notice).toBeVisible()
    await expect(notice.getByRole('button', { name: 'Try again' })).toBeVisible()
  })
})
