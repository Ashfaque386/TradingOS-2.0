#!/usr/bin/env node
// Build Spec §21-22 hardening pass: a real load smoke test for WebSocket
// fan-out under several simultaneous Console connections (low priority at
// solo-operator scale per that pass's own wording, but confirm it doesn't
// fall over) -- opens real concurrent connections against the actual
// running backend's two DB-polling channels (sign-off-queue,
// activity-feed; see src/api/routes/websockets.py) and checks every one
// receives live snapshots and the backend survives with headroom to
// spare, not just that it doesn't immediately crash.
//
// This is also a direct regression check for a real bug this hardening
// pass found and fixed earlier: enough *dangling* (never-closed)
// WebSocket connections against these same channels exhausted
// SQLAlchemy's connection pool (QueuePool limit of size 5 overflow 10)
// and 500'd every request, including plain HTTP ones. This script proves
// a bounded, promptly-closed batch of connections does NOT reproduce
// that -- i.e. the earlier failure was about unbounded accumulation
// (dangling connections nothing ever closes), not a fundamental flaw
// in fan-out under ordinary load.
//
// Usage: node scripts/ws-fanout-load-smoke-test.mjs
// Env: E2E_API_URL (default http://localhost:8000), CONNECTIONS_PER_CHANNEL (default 15)

const API_URL = process.env.E2E_API_URL ?? 'http://localhost:8000'
const WS_URL = API_URL.replace(/^http/, 'ws')
const CONNECTIONS_PER_CHANNEL = Number(process.env.CONNECTIONS_PER_CHANNEL ?? 15)
const HOLD_OPEN_MS = 8_000

// sign-off-queue is a periodic full-snapshot broadcaster (see
// _SIGNOFF_POLL_INTERVAL_SECONDS = 2.0 in websockets.py) -- an 8s hold
// should see several snapshots regardless of activity. activity-feed (and
// agent-logs) is a delta/diff stream instead: one `backfill` message on
// connect, then only `event` messages for genuinely NEW AuditLog rows --
// with no new activity during a passive load test's hold window, exactly
// one message (the backfill) is the correct, expected outcome, not a
// failure. Per-channel minimums reflect that real difference in design
// rather than asserting a single uniform threshold across both.
const MIN_MESSAGES_BY_CHANNEL = {
  '/api/v1/ws/sign-off-queue': 2,
  '/api/v1/ws/activity-feed': 1,
}

const CHANNELS = Object.keys(MIN_MESSAGES_BY_CHANNEL)

async function login(email, password) {
  const res = await fetch(`${API_URL}/api/v1/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  if (!res.ok) throw new Error(`login failed: ${res.status} ${await res.text()}`)
  const body = await res.json()
  return body.access_token
}

function openConnection(path, token) {
  return new Promise((resolve) => {
    const ws = new WebSocket(`${WS_URL}${path}?token=${token}`)
    let messageCount = 0
    let opened = false
    let erroredOut = false

    const closeTimer = setTimeout(() => {
      ws.close()
    }, HOLD_OPEN_MS)

    ws.onopen = () => {
      opened = true
    }
    ws.onmessage = () => {
      messageCount++
    }
    ws.onerror = () => {
      erroredOut = true
    }
    ws.onclose = (event) => {
      clearTimeout(closeTimer)
      resolve({ path, opened, messageCount, erroredOut, closeCode: event.code })
    }
  })
}

async function checkHealth(label) {
  const res = await fetch(`${API_URL}/health`, { signal: AbortSignal.timeout(5000) })
  const ok = res.status === 200
  console.log(`[health] ${label}: ${ok ? 'OK' : `FAILED (status ${res.status})`}`)
  return ok
}

async function main() {
  console.log(
    `Opening ${CONNECTIONS_PER_CHANNEL} connections each to ${CHANNELS.length} channels ` +
      `(${CONNECTIONS_PER_CHANNEL * CHANNELS.length} total) against ${API_URL}...`,
  )

  const healthyBefore = await checkHealth('before')
  if (!healthyBefore) {
    console.error('Backend is not healthy before the test even started -- aborting.')
    process.exit(1)
  }

  const token = await login('e2e.risk@example.com', 'E2eRiskPass!2026')

  const connectionPromises = []
  for (const path of CHANNELS) {
    for (let i = 0; i < CONNECTIONS_PER_CHANNEL; i++) {
      connectionPromises.push(openConnection(path, token))
    }
  }

  const results = await Promise.all(connectionPromises)

  const healthyDuring = await checkHealth('immediately after close')

  // Give the pool a beat to fully reclaim connections, then confirm the
  // backend can still serve a brand new request end-to-end (login + one
  // more short-lived WS connection), not just /health.
  await new Promise((r) => setTimeout(r, 1000))
  const freshToken = await login('e2e.admin@example.com', 'E2eAdminPass!2026')
  const followUp = await openConnection('/api/v1/ws/activity-feed', freshToken)
  const healthyAfterFollowUp = await checkHealth('after a fresh follow-up connection')

  let failures = 0
  for (const r of results) {
    const minExpected = MIN_MESSAGES_BY_CHANNEL[r.path]
    const ok = r.opened && !r.erroredOut && r.messageCount >= minExpected
    if (!ok) failures++
  }
  console.log(
    `\n${results.length - failures}/${results.length} connections opened cleanly and received ` +
      `at least their channel's expected minimum message count.`,
  )
  for (const path of CHANNELS) {
    const forPath = results.filter((r) => r.path === path)
    const counts = forPath.map((r) => r.messageCount)
    console.log(`  ${path}: message counts = [${counts.join(', ')}]`)
  }
  console.log(
    `Follow-up connection: opened=${followUp.opened} messages=${followUp.messageCount} ` +
      `errored=${followUp.erroredOut}`,
  )

  const passed =
    failures === 0 && healthyBefore && healthyDuring && healthyAfterFollowUp && followUp.opened

  console.log(passed ? '\nPASS' : '\nFAIL')
  process.exit(passed ? 0 : 1)
}

main().catch((err) => {
  console.error('Load smoke test crashed:', err)
  process.exit(1)
})
