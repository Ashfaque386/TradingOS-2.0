#!/usr/bin/env node
// Cross-platform wrapper around `docker compose up`: if API_HOST_PORT
// (default 8000) or FRONTEND_HOST_PORT (default 3000) is already taken,
// automatically bumps to the next port and retries -- no manual
// intervention needed. Run as `pnpm docker:up` (extra args, e.g. `-d`,
// are passed through to `docker compose up`).
//
// This retries against docker compose's *actual* failure rather than
// pre-checking port availability with a plain TCP bind test: on Windows
// (Docker Desktop / WSL2), a port can be reserved in Docker's own network
// layer without showing up as "in use" to a normal OS-level socket bind,
// so a pre-check can wrongly report a busy port as free. Retrying on the
// real error is the only reliable source of truth.
//
// It also does not trust a 0 exit code alone: `docker compose up -d`
// has been observed to exit 0 while a container's port silently failed to
// publish (confirmed live), so every "success" is double-checked with
// `docker port <container> <port>` before being declared real.
'use strict'

const { spawn } = require('child_process')

const CONTAINERS = {
  backend: { name: 'TradingOS-2.0-Backend', containerPort: 8000 },
  frontend: { name: 'TradingOS-2.0-Frontend', containerPort: 3000 },
}

function runDockerComposeUp(apiPort, frontendPort, extraArgs) {
  return new Promise((resolve) => {
    const env = {
      ...process.env,
      API_HOST_PORT: String(apiPort),
      FRONTEND_HOST_PORT: String(frontendPort),
    }
    const child = spawn('docker', ['compose', 'up', ...extraArgs], {
      env,
      shell: process.platform === 'win32',
    })

    let output = ''
    child.stdout.on('data', (chunk) => {
      process.stdout.write(chunk)
      output += chunk.toString()
    })
    child.stderr.on('data', (chunk) => {
      process.stderr.write(chunk)
      output += chunk.toString()
    })
    child.on('error', (err) => {
      resolve({ code: 1, output: `${output}\n${err.message}` })
    })
    child.on('exit', (code) => resolve({ code: code ?? 1, output }))
  })
}

function isPortPublished(containerName, containerPort) {
  return new Promise((resolve) => {
    const child = spawn('docker', ['port', containerName, String(containerPort)], {
      shell: process.platform === 'win32',
    })
    let output = ''
    child.stdout.on('data', (chunk) => {
      output += chunk.toString()
    })
    child.on('error', () => resolve(false))
    child.on('exit', () => resolve(output.trim().length > 0))
  })
}

function parsePortConflict(output) {
  const match = output.match(/Bind for [\d.]+:(\d+) failed: port is already allocated/)
  if (!match) return null
  return {
    port: Number(match[1]),
    isFrontend: /TradingOS-2\.0-Frontend/i.test(output),
    isBackend: /TradingOS-2\.0-Backend/i.test(output),
  }
}

function removeContainer(service) {
  return new Promise((resolve) => {
    const child = spawn('docker', ['compose', 'rm', '-f', service], {
      env: process.env,
      shell: process.platform === 'win32',
      stdio: 'ignore',
    })
    child.on('error', () => resolve())
    child.on('exit', () => resolve())
  })
}

async function main() {
  let apiPort = Number(process.env.API_HOST_PORT) || 8000
  let frontendPort = Number(process.env.FRONTEND_HOST_PORT) || 3000
  const extraArgs = process.argv.slice(2)
  const maxAttempts = 20

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    console.log(`[docker:up] Attempt ${attempt}: backend=${apiPort} frontend=${frontendPort}`)
    const { code, output } = await runDockerComposeUp(apiPort, frontendPort, extraArgs)

    if (code === 0) {
      const [backendOk, frontendOk] = await Promise.all([
        isPortPublished(CONTAINERS.backend.name, CONTAINERS.backend.containerPort),
        isPortPublished(CONTAINERS.frontend.name, CONTAINERS.frontend.containerPort),
      ])

      if (backendOk && frontendOk) {
        console.log(`[docker:up] Backend:  http://localhost:${apiPort}`)
        console.log(`[docker:up] Frontend: http://localhost:${frontendPort}`)
        process.exit(0)
      }

      if (!frontendOk) {
        frontendPort += 1
        console.log(`[docker:up] Frontend port didn't actually publish — bumping to ${frontendPort} and retrying...`)
        await removeContainer('frontend')
      } else {
        apiPort += 1
        console.log(`[docker:up] Backend port didn't actually publish — bumping to ${apiPort} and retrying...`)
        await removeContainer('backend')
      }
      continue
    }

    const conflict = parsePortConflict(output)
    if (!conflict) {
      console.error('[docker:up] docker compose up failed for a reason other than a port conflict — not retrying.')
      process.exit(code)
    }

    if (conflict.isFrontend || conflict.port === frontendPort) {
      frontendPort += 1
      console.log(`[docker:up] Port busy — bumping frontend to ${frontendPort} and retrying...`)
      await removeContainer('frontend')
    } else if (conflict.isBackend || conflict.port === apiPort) {
      apiPort += 1
      console.log(`[docker:up] Port busy — bumping backend to ${apiPort} and retrying...`)
      await removeContainer('backend')
    } else {
      console.error(`[docker:up] Port conflict on ${conflict.port} but could not tell which service — aborting.`)
      process.exit(code)
    }
  }

  console.error(`[docker:up] Still failing after ${maxAttempts} attempts.`)
  process.exit(1)
}

main()
