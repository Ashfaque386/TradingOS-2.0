import { expect, test } from '@playwright/test'
import { E2E_USERS, login, API_BASE_URL, tokenFor } from './utils'

// Settings > Notification Channels: a failed Disconnect or alert-level change must
// say so. These calls used to have no error handling at all -- a refusal (the
// channel already gone in another tab, a server error) surfaced as an unhandled
// rejection and a card that silently kept showing the old state, and a failed
// alert-level toggle stayed ticked as if it had been saved.
//
// Saving a webhook only stores it (nothing is sent), so a Slack channel can be
// configured through the API without any network access. The failures themselves
// are forced by intercepting the request: this is about how the page reacts.

const WEBHOOK = 'https://hooks.slack.com/services/T000/B000/e2e-not-a-real-webhook'

async function adminToken(): Promise<string> {
  return tokenFor(E2E_USERS.admin)
}

async function configureSlack(token: string) {
  const res = await fetch(`${API_BASE_URL}/api/v1/notification-channels/slack`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ webhook_url: WEBHOOK }),
  })
  expect(res.status).toBe(204)
}

async function removeSlack(token: string) {
  await fetch(`${API_BASE_URL}/api/v1/notification-channels/slack`, {
    method: 'DELETE',
    headers: { Authorization: `Bearer ${token}` },
  })
}

const slackCard = (page: import('@playwright/test').Page) =>
  page.locator('.set-channel').filter({ hasText: /slack/i }).first()

test('a Disconnect the server refuses says why, and the card is refreshed', async ({ page }) => {
  const token = await adminToken()
  await removeSlack(token)
  await configureSlack(token)
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=notifications')
    await page.route('**/api/v1/notification-channels/slack', async (route) => {
      if (route.request().method() === 'DELETE') {
        await route.fulfill({
          status: 404,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'the slack channel is not configured' }),
        })
      } else {
        await route.continue()
      }
    })

    await slackCard(page).getByRole('button', { name: 'Disconnect' }).click()

    await expect(slackCard(page).getByRole('alert')).toHaveText('the slack channel is not configured')
  } finally {
    await removeSlack(token)
  }
})

test('an alert-level change that fails to save is shown as not saved', async ({ page }) => {
  const token = await adminToken()
  await removeSlack(token)
  await configureSlack(token)
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=notifications')
    const toggle = slackCard(page).getByRole('button', { name: /on slack$/ }).first()
    const before = await toggle.getAttribute('aria-label')
    await page.route('**/api/v1/notification-channels/slack', async (route) => {
      // The toggle saves with PATCH (settings only, no secret can travel with it).
      if (route.request().method() === 'PATCH') {
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'boom' }) })
      } else {
        await route.continue()
      }
    })

    await toggle.click()

    await expect(slackCard(page).getByRole('alert')).toBeVisible()
    // Not left showing a change that was never saved.
    await expect(toggle).toHaveAttribute('aria-label', before as string)
  } finally {
    await removeSlack(token)
  }
})

test('an alert-level change is saved without losing the channel', async ({ page }) => {
  const token = await adminToken()
  await removeSlack(token)
  await configureSlack(token)
  try {
    await login(page, E2E_USERS.admin)
    await page.goto('/settings?section=notifications')
    const toggle = slackCard(page).getByRole('button', { name: /on slack$/ }).first()

    await toggle.click()

    await expect
      .poll(async () => {
        const res = await fetch(`${API_BASE_URL}/api/v1/notification-channels`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        const channels = (await res.json()) as { channel: string; configured: boolean; alert_levels: string[] }[]
        const slack = channels.find((c) => c.channel === 'slack')
        return slack ? { configured: slack.configured, levels: slack.alert_levels.length } : null
      })
      .toEqual({ configured: true, levels: 1 })
  } finally {
    await removeSlack(token)
  }
})
