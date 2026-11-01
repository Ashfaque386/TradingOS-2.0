/** @type {import('next').NextConfig} */

// Sent with every response, static files included. The Content-Security-Policy (which needs a nonce per
// response) is set by proxy.ts. Strict-Transport-Security is deliberately not here: it belongs where TLS
// ends (a reverse proxy or the platform), because a browser that sees it refuses plain HTTP to this host
// for a year.
const securityHeaders = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'X-Frame-Options', value: 'DENY' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'Permissions-Policy', value: 'camera=(), microphone=(), geolocation=()' },
  { key: 'Cross-Origin-Opener-Policy', value: 'same-origin' },
]

const nextConfig = {
  // Only for the root Dockerfile's build — Vercel (which sets VERCEL=1
  // during its own builds) manages its own serverless output and this
  // conflicts with it, so never set it there.
  ...(process.env.VERCEL ? {} : { output: 'standalone' }),
  typescript: {
    ignoreBuildErrors: false,
  },
  images: {
    unoptimized: true,
  },
  // The framework announces itself on every response; nothing needs to know which one it is.
  poweredByHeader: false,
  async headers() {
    return [{ source: '/:path*', headers: securityHeaders }]
  },
}

export default nextConfig
