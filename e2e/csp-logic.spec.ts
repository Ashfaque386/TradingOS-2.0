import { expect, test } from '@playwright/test'
import { buildCsp, makeNonce, originOf } from '../lib/csp'

// The Content-Security-Policy the console sends (lib/csp.ts). No browser, no backend.

const policy = (over: Partial<Parameters<typeof buildCsp>[0]> = {}) =>
  buildCsp({ nonce: 'abc123', isDev: false, apiUrl: 'http://localhost:8000', wsUrl: 'ws://localhost:8000', host: 'localhost:3000', ...over })

const directive = (csp: string, name: string) =>
  csp
    .split('; ')
    .find((d) => d.startsWith(`${name} `))
    ?.slice(name.length + 1)

test.describe('scripts', () => {
  test('may run only from this origin or with this response nonce, and never inline without it', () => {
    const scripts = directive(policy(), 'script-src')

    expect(scripts).toBe("'self' 'nonce-abc123' 'strict-dynamic'")
    expect(scripts).not.toContain('unsafe-inline')
    expect(scripts).not.toContain('unsafe-eval')
  })

  test('the dev server also gets eval, which it needs for hot reload', () => {
    expect(directive(policy({ isDev: true }), 'script-src')).toContain("'unsafe-eval'")
  })

  test('carry a different nonce for each response', () => {
    const nonces = new Set(Array.from({ length: 20 }, () => makeNonce()))

    expect(nonces.size).toBe(20)
    for (const nonce of nonces) expect(nonce.length).toBeGreaterThanOrEqual(40)
  })
})

test.describe('connections', () => {
  test('go to this origin, the API and its WebSocket, and this host\'s own WebSocket', () => {
    expect(directive(policy(), 'connect-src')).toBe(
      "'self' http://localhost:8000 ws://localhost:8000 ws://localhost:3000 wss://localhost:3000",
    )
  })

  test('with no WebSocket address configured the API\'s own is allowed, as the console opens it there', () => {
    expect(directive(policy({ wsUrl: undefined, host: undefined }), 'connect-src')).toBe(
      "'self' http://localhost:8000 ws://localhost:8000",
    )
    expect(directive(policy({ apiUrl: 'https://api.example.com', wsUrl: '', host: undefined }), 'connect-src')).toBe(
      "'self' https://api.example.com wss://api.example.com",
    )
  })

  test('with no API configured (served from the API\'s own origin) are this origin only', () => {
    expect(directive(policy({ apiUrl: '', wsUrl: '', host: undefined }), 'connect-src')).toBe("'self'")
  })

  test('are not repeated when the API is on this host', () => {
    const connect = directive(policy({ apiUrl: 'http://localhost:3000', wsUrl: 'ws://localhost:3000' }), 'connect-src')!

    expect(connect.split(' ').filter((s) => s === 'ws://localhost:3000')).toHaveLength(1)
  })

  test('are not widened by a configured URL that is not a URL', () => {
    expect(directive(policy({ apiUrl: 'not a url', wsUrl: undefined, host: undefined }), 'connect-src')).toBe("'self'")
  })
})

test.describe('everything else', () => {
  test('nothing may be framed, embedded as an object, or re-based', () => {
    const csp = policy()

    expect(directive(csp, 'frame-ancestors')).toBe("'none'")
    expect(directive(csp, 'object-src')).toBe("'none'")
    expect(directive(csp, 'base-uri')).toBe("'self'")
    expect(directive(csp, 'form-action')).toBe("'self'")
    expect(directive(csp, 'default-src')).toBe("'self'")
  })

  test('styles may be inline (the console sets style attributes) but come from nowhere else', () => {
    expect(directive(policy(), 'style-src')).toBe("'self' 'unsafe-inline'")
  })

  test('images and fonts: this origin, plus inline data', () => {
    expect(directive(policy(), 'img-src')).toBe("'self' data: blob:")
    expect(directive(policy(), 'font-src')).toBe("'self' data:")
  })
})

test.describe('the origin of a configured URL', () => {
  test('drops the path and keeps the port', () => {
    expect(originOf('http://localhost:8000/api/v1')).toBe('http://localhost:8000')
    expect(originOf('wss://api.example.com/ws')).toBe('wss://api.example.com')
  })

  test('is nothing for an empty or unusable value', () => {
    expect(originOf('')).toBeUndefined()
    expect(originOf(undefined)).toBeUndefined()
    expect(originOf('localhost:8000')).toBeUndefined()
    expect(originOf('nonsense')).toBeUndefined()
  })
})
