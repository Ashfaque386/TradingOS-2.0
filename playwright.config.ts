import { defineConfig, devices } from '@playwright/test'

// Build Spec §21-22 hardening pass: a real Playwright suite against the
// actual FastAPI backend + Next.js frontend + Postgres/Redis, not mocked
// network responses. This repo's sandbox has no reachable Docker daemon
// (see backend/src/engine/sandbox/factory.py's own gVisor fallback for
// the same constraint elsewhere), so these tests run against services
// started directly on the host (see e2e/README.md) rather than
// `docker compose up`. baseURL/API port here must match e2e/README.md.
export default defineConfig({
  testDir: './e2e',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  retries: 0,
  workers: 1,
  reporter: [['list']],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://localhost:3010',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH ?? '/opt/pw-browsers/chromium',
    },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
