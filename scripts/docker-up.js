#!/usr/bin/env node
// Cross-platform wrapper around `docker compose up`: if API_HOST_PORT
// (default 8000) or FRONTEND_HOST_PORT (default 3000) is already taken by
// something else on the host, walk forward to the next free port instead
// of failing with "port is already allocated". Run as `pnpm docker:up`
// (extra args, e.g. `-d`, are passed through to `docker compose up`).
'use strict'

const net = require('net')
const { spawn } = require('child_process')

function isPortFree(port) {
  return new Promise((resolve) => {
    const server = net.createServer()
    server.once('error', () => resolve(false))
    server.once('listening', () => server.close(() => resolve(true)))
    server.listen(port, '0.0.0.0')
  })
}

async function findFreePort(startPort, taken, maxAttempts = 50) {
  for (let port = startPort; port < startPort + maxAttempts; port++) {
    if (taken.has(port)) continue
    if (await isPortFree(port)) return port
  }
  throw new Error(`No free port found starting at ${startPort} after ${maxAttempts} attempts`)
}

async function main() {
  const apiBase = Number(process.env.API_HOST_PORT) || 8000
  const frontendBase = Number(process.env.FRONTEND_HOST_PORT) || 3000

  const taken = new Set()
  const apiPort = await findFreePort(apiBase, taken)
  taken.add(apiPort)
  const frontendPort = await findFreePort(frontendBase, taken)

  if (apiPort !== apiBase) {
    console.log(`[docker:up] Port ${apiBase} is busy — backend will use ${apiPort} instead.`)
  }
  if (frontendPort !== frontendBase) {
    console.log(`[docker:up] Port ${frontendBase} is busy — frontend will use ${frontendPort} instead.`)
  }
  console.log(`[docker:up] Backend:  http://localhost:${apiPort}`)
  console.log(`[docker:up] Frontend: http://localhost:${frontendPort}`)

  const env = {
    ...process.env,
    API_HOST_PORT: String(apiPort),
    FRONTEND_HOST_PORT: String(frontendPort),
  }
  const args = ['compose', 'up', ...process.argv.slice(2)]
  const child = spawn('docker', args, {
    stdio: 'inherit',
    env,
    shell: process.platform === 'win32',
  })
  child.on('error', (err) => {
    console.error('[docker:up] Failed to launch docker:', err.message)
    process.exit(1)
  })
  child.on('exit', (code) => process.exit(code ?? 0))
}

main().catch((err) => {
  console.error(`[docker:up] ${err.message}`)
  process.exit(1)
})
