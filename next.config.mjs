/** @type {import('next').NextConfig} */
const nextConfig = {
  // Only for the root Dockerfile's build — Vercel (which sets VERCEL=1
  // during its own builds) manages its own serverless output and this
  // conflicts with it, so never set it there.
  ...(process.env.VERCEL ? {} : { output: 'standalone' }),
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },
}

export default nextConfig
