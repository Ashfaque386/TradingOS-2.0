# Frontend image (the v0-built Next.js console). Built and run entirely
# separately from backend/Dockerfile's Postgres+API image — see
# docker-compose.yml's "frontend" service. `pnpm dev` (see README.md) is
# for local iteration when pushing changes from v0; this image is how the
# app actually runs via `docker compose up`.
FROM node:24-alpine AS base
WORKDIR /app
# Next.js on Alpine needs libc6-compat for a few native deps it traces in.
RUN apk add --no-cache libc6-compat \
    && corepack enable

FROM base AS deps
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

FROM base AS build
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* vars are inlined into the client JS bundle at build time,
# not read at container start -- they must be ARGs threaded through to a
# build-time ENV, not just container ENV like the vars above. The browser
# (not this container) uses these to reach the backend, so they default to
# the backend's published host port, not its internal Docker hostname.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_WS_URL=ws://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_WS_URL=${NEXT_PUBLIC_WS_URL}
RUN pnpm build

# Standalone output (next.config.mjs: output: 'standalone') copies only the
# traced production dependencies, not the full node_modules tree.
FROM node:24-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
