// The Content-Security-Policy for one response of the console (applied by /proxy.ts).
//
// Scripts are the part that matters: only the scripts Next itself renders for this response may run
// (each carries this response's random nonce, and 'strict-dynamic' lets those load their own chunks).
// An attacker who gets markup into a page can add a <script> tag but cannot guess the nonce, and cannot
// load one from another host. Styles keep 'unsafe-inline' because the console sets inline style
// attributes and the chart theme is a <style> element; a style cannot run code, which is why that is the
// concession and scripts are not.
//
// connect-src names the API (and its WebSocket) the build was pointed at: the page may talk to those and
// to itself, nowhere else, so a script that did run could not post what it read to another host.

export type CspInput = {
  /** Random, different for every response. */
  nonce: string
  /** The dev server needs 'unsafe-eval' (React's debugging, hot reload); a production build does not. */
  isDev: boolean
  /** NEXT_PUBLIC_API_URL, e.g. http://localhost:8000. Empty for a console served from the API's own origin. */
  apiUrl?: string
  /** NEXT_PUBLIC_WS_URL, e.g. ws://localhost:8000. When unset the console opens its sockets on the API's
   * address with the scheme swapped (lib/ws.ts), and the policy follows the same rule. */
  wsUrl?: string
  /** The host (with port) the page was requested from: its own WebSocket. */
  host?: string
}

/** `scheme://host:port` of a URL, or undefined for an empty or unparseable one. */
export function originOf(url: string | undefined): string | undefined {
  if (!url) return undefined
  try {
    const parsed = new URL(url)
    return parsed.origin === 'null' ? undefined : parsed.origin
  } catch {
    return undefined
  }
}

/** A fresh nonce: unguessable and different for every response. */
export function makeNonce(): string {
  return btoa(crypto.randomUUID())
}

export function buildCsp({ nonce, isDev, apiUrl, wsUrl, host }: CspInput): string {
  const connect = new Set<string>(["'self'"])
  const api = originOf(apiUrl)
  const ws = originOf(wsUrl) ?? api?.replace(/^http/, 'ws')
  if (api) connect.add(api)
  if (ws) connect.add(ws)
  if (host) {
    // 'self' covers a same-origin WebSocket in current browsers; older ones need it spelled out.
    connect.add(`ws://${host}`)
    connect.add(`wss://${host}`)
  }

  const directives: string[][] = [
    ['default-src', "'self'"],
    ['script-src', "'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(isDev ? ["'unsafe-eval'"] : [])],
    ['style-src', "'self'", "'unsafe-inline'"],
    ['img-src', "'self'", 'data:', 'blob:'],
    ['font-src', "'self'", 'data:'],
    ['connect-src', ...connect],
    ['object-src', "'none'"],
    ['base-uri', "'self'"],
    ['form-action', "'self'"],
    ['frame-ancestors', "'none'"],
  ]
  return directives.map((parts) => parts.join(' ')).join('; ')
}
