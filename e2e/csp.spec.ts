import { expect, test } from '@playwright/test'
import { E2E_USERS, login } from './utils'

// The Content-Security-Policy (proxy.ts, lib/csp.ts) is only worth having if the console still works
// under it. A policy that blocks the app's own script, style, image or connection shows up as a
// `securitypolicyviolation` event and a console error: this visits every page and fails on the first
// one. (The E2E run uses the dev server, which also allows 'unsafe-eval'; the production build's policy
// is the same minus that.)

type Violations = { __cspViolations: string[] }

const PAGES = [
  '/',
  '/mission-control',
  '/strategies',
  '/backtests',
  '/orders',
  '/portfolio',
  '/paper-trading',
  '/agent-fleet',
  '/analysis',
  '/audit',
  '/chat',
  '/investor-reports',
  '/settings',
  '/settings?section=users',
  '/settings?section=security',
]

test('no page runs into its own Content-Security-Policy', async ({ page }) => {
  test.setTimeout(300_000) // the dev server compiles each page the first time it is visited
  const consoleReports: string[] = []
  page.on('console', (message) => {
    if (/Content Security Policy|violates the following/i.test(message.text())) {
      consoleReports.push(`${page.url()}: ${message.text()}`)
    }
  })
  await page.addInitScript(() => {
    const store = window as unknown as Violations
    store.__cspViolations = []
    document.addEventListener('securitypolicyviolation', (event) => {
      store.__cspViolations.push(`${event.violatedDirective} blocked ${event.blockedURI} (${event.sourceFile}:${event.lineNumber})`)
    })
  })

  await login(page, E2E_USERS.admin)
  const violations: string[] = []
  for (const path of PAGES) {
    await page.goto(path)
    await expect(page.getByRole('button', { name: E2E_USERS.admin.email, exact: true })).toBeVisible({ timeout: 20_000 })
    await page.waitForTimeout(1_500) // let the page's own requests and sockets start
    const found = await page.evaluate(() => (window as unknown as Violations).__cspViolations)
    violations.push(...found.map((v) => `${path}: ${v}`))
  }

  expect(violations).toEqual([])
  expect(consoleReports).toEqual([])
})

test('a page carries a policy with its own nonce, different each time, and the scripts use it', async ({ page }) => {
  await page.context().clearCookies()
  const nonceOf = (policy: string | undefined) => /'nonce-([^']+)'/.exec(policy ?? '')?.[1]

  const first = await page.goto('/login')
  const second = await page.goto('/login')

  const policy = first!.headers()['content-security-policy']
  expect(policy).toContain("default-src 'self'")
  expect(policy).toContain("frame-ancestors 'none'")
  expect(policy).toContain("object-src 'none'")
  expect(policy).not.toMatch(/script-src[^;]*'unsafe-inline'/)
  expect(nonceOf(policy)).toBeTruthy()
  expect(nonceOf(policy)).not.toEqual(nonceOf(second!.headers()['content-security-policy']))

  // Next put this response's nonce on the scripts it rendered.
  const used = await page.evaluate(() => [...document.scripts].map((s) => s.nonce).filter(Boolean))
  expect(used.length).toBeGreaterThan(0)
  expect(new Set(used)).toEqual(new Set([nonceOf(second!.headers()['content-security-policy'])]))
})

test('a page also carries the plain headers, and does not name its framework', async ({ page }) => {
  const response = await page.goto('/login')
  const headers = response!.headers()

  expect(headers['x-content-type-options']).toBe('nosniff')
  expect(headers['x-frame-options']).toBe('DENY')
  expect(headers['referrer-policy']).toBe('strict-origin-when-cross-origin')
  expect(headers['cross-origin-opener-policy']).toBe('same-origin')
  expect(headers['permissions-policy']).toContain('camera=()')
  expect(headers['x-powered-by']).toBeUndefined()
})
