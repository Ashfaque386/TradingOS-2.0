#!/usr/bin/env node
// Cross-platform wrapper around `docker compose up`: if any of this
// stack's four published host ports (backend/frontend/prometheus/grafana)
// is already taken by something else on the machine, automatically bumps
// that one port and retries -- no manual intervention needed. Run as
// `pnpm docker:up` (extra args, e.g. `-d`, are passed through to
// `docker compose up`).
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

// One entry per service this stack publishes a host port for. `envVar` is
// what docker-compose.yml's `${...}` substitution reads; `composeService`
// is the service name `docker compose rm -f <name>` takes.
const SERVICES = [
  {
    key: 'backend',
    containerName: 'TradingOS-2.0-Backend',
    containerPort: 8000,
    envVar: 'API_HOST_PORT',
    composeService: 'backend',
    defaultPort: 8000,
  },
  {
    key: 'frontend',
    containerName: 'TradingOS-2.0-Frontend',
    containerPort: 3000,
    envVar: 'FRONTEND_HOST_PORT',
    composeService: 'frontend',
    defaultPort: 3000,
  },
  {
    key: 'prometheus',
    containerName: 'TradingOS-2.0-Prometheus',
    containerPort: 9090,
    envVar: 'PROMETHEUS_HOST_PORT',
    composeService: 'prometheus',
    defaultPort: 9090,
  },
  {
    key: 'grafana',
    containerName: 'TradingOS-2.0-Grafana',
    containerPort: 3000,
    envVar: 'GRAFANA_HOST_PORT',
    composeService: 'grafana',
    defaultPort: 3001,
  },
]

function runDockerComposeUp(ports, extraArgs) {
  return new Promise((resolve) => {
    const env = { ...process.env }
    for (const service of SERVICES) {
      env[service.envVar] = String(ports[service.key])
    }
    // Without --build, `docker compose up` reuses an already-built image
    // untouched even when a build ARG (e.g. frontend's NEXT_PUBLIC_API_URL,
    // derived from API_HOST_PORT in docker-compose.yml) would now resolve
    // differently -- so a frontend image built once against port 8000
    // would keep silently pointing at 8000 forever, even after this
    // script bumps the backend to 8001 on a later run. --build makes
    // Compose re-evaluate build args every invocation; Docker's layer
    // cache keeps this cheap when nothing actually changed.
    const args = extraArgs.includes('--build') ? extraArgs : ['--build', ...extraArgs]
    const child = spawn('docker', ['compose', 'up', ...args], {
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
  // Only the specific line naming the failing endpoint may be checked for
  // which service it is — `docker compose up`'s surrounding output lists
  // *every* service's container transitioning through
  // Creating/Starting/Started (including every container name this stack
  // has) regardless of which one's port actually conflicted, so matching
  // against the whole captured blob previously misattributed any
  // conflict to whichever service happened to be mentioned first (nearly
  // always the frontend) and only ever bumped that one port, silently
  // never fixing the real conflict.
  const conflictLine = output.split('\n').find((line) => /port is already allocated/.test(line))
  if (!conflictLine) return null
  const match = conflictLine.match(/Bind for [\d.]+:(\d+) failed: port is already allocated/)
  if (!match) return null
  const port = Number(match[1])
  const service = SERVICES.find((s) => new RegExp(s.containerName.replace(/\./g, '\\.'), 'i').test(conflictLine))
  return { port, serviceKey: service ? service.key : null }
}

function removeContainer(composeService) {
  return new Promise((resolve) => {
    const child = spawn('docker', ['compose', 'rm', '-f', composeService], {
      env: process.env,
      shell: process.platform === 'win32',
      stdio: 'ignore',
    })
    child.on('error', () => resolve())
    child.on('exit', () => resolve())
  })
}

async function main() {
  const ports = {}
  for (const service of SERVICES) {
    ports[service.key] = Number(process.env[service.envVar]) || service.defaultPort
  }
  const extraArgs = process.argv.slice(2)
  const maxAttempts = 20

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    console.log(
      `[docker:up] Attempt ${attempt}: ${SERVICES.map((s) => `${s.key}=${ports[s.key]}`).join(' ')}`
    )
    const { code, output } = await runDockerComposeUp(ports, extraArgs)

    if (code === 0) {
      const publishedChecks = await Promise.all(
        SERVICES.map((s) => isPortPublished(s.containerName, s.containerPort))
      )
      const notPublished = SERVICES.filter((_, i) => !publishedChecks[i])

      if (notPublished.length === 0) {
        for (const service of SERVICES) {
          console.log(`[docker:up] ${service.key}: http://localhost:${ports[service.key]}`)
        }
        process.exit(0)
      }

      const service = notPublished[0]
      ports[service.key] += 1
      console.log(
        `[docker:up] ${service.key} port didn't actually publish — bumping to ${ports[service.key]} and retrying...`
      )
      await removeContainer(service.composeService)
      continue
    }

    const conflict = parsePortConflict(output)
    if (!conflict) {
      console.error('[docker:up] docker compose up failed for a reason other than a port conflict — not retrying.')
      process.exit(code)
    }

    const service =
      SERVICES.find((s) => s.key === conflict.serviceKey) ??
      SERVICES.find((s) => ports[s.key] === conflict.port)

    if (service) {
      ports[service.key] += 1
      console.log(`[docker:up] Port busy — bumping ${service.key} to ${ports[service.key]} and retrying...`)
      await removeContainer(service.composeService)
    } else {
      console.error(`[docker:up] Port conflict on ${conflict.port} but could not tell which service — aborting.`)
      process.exit(code)
    }
  }

  console.error(`[docker:up] Still failing after ${maxAttempts} attempts.`)
  process.exit(1)
}

main()
