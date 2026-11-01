import { type NextRequest, NextResponse } from 'next/server'
import { buildCsp, makeNonce } from '@/lib/csp'

// Runs in front of every page and gives it a Content-Security-Policy with a fresh nonce (lib/csp.ts says
// what the policy is and why). Next reads the nonce back out of the request's policy and puts it on the
// scripts it renders, which is why the root layout renders per request (app/layout.tsx).
export function proxy(request: NextRequest) {
  const nonce = makeNonce()
  const policy = buildCsp({
    nonce,
    isDev: process.env.NODE_ENV === 'development',
    apiUrl: process.env.NEXT_PUBLIC_API_URL,
    wsUrl: process.env.NEXT_PUBLIC_WS_URL,
    host: request.headers.get('host') ?? undefined,
  })

  const requestHeaders = new Headers(request.headers)
  requestHeaders.set('x-nonce', nonce)
  requestHeaders.set('Content-Security-Policy', policy)

  const response = NextResponse.next({ request: { headers: requestHeaders } })
  response.headers.set('Content-Security-Policy', policy)
  return response
}

export const config = {
  matcher: [
    {
      // Pages only: not the build's static files, images or icons, which a policy does nothing for.
      source: '/((?!_next/static|_next/image|favicon.ico|icon|apple-icon|.*\\.(?:png|svg|jpg|jpeg|gif|webp|ico)$).*)',
      missing: [
        { type: 'header', key: 'next-router-prefetch' },
        { type: 'header', key: 'purpose', value: 'prefetch' },
      ],
    },
  ],
}
