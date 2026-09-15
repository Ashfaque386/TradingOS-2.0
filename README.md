# TradingOS-2.0-yp

This is a [Next.js](https://nextjs.org) project bootstrapped with [v0](https://v0.app).

## Built with v0

This repository is linked to a [v0](https://v0.app) project. You can continue developing by visiting the link below -- start new chats to make changes, and v0 will push commits directly to this repo. Every merge to `main` will automatically deploy.

[Continue working on v0 →](https://v0.app/chat/projects/prj_uaBySbq9NwBHR5iBUb2xizhzuSAy)

## Getting Started

First, run the development server:

```bash
npm run dev
# or
yarn dev
# or
pnpm dev
```

Open [http://localhost:3000](http://localhost:3000) with your browser to see the result.

You can start editing the page by modifying `app/page.tsx`. The page auto-updates as you edit the file.

## Running the full app in Docker

`docker compose up` builds and runs the frontend (this Next.js app) and the backend together — see the repo-root `Dockerfile` and `docker-compose.yml`, and `docs/CLAUDE.md` for the full architecture.

If port 8000 or 3000 is already taken by something else on your machine, use:

```bash
pnpm docker:up
```

instead of `docker compose up` directly — it checks whether `API_HOST_PORT` (default 8000) and `FRONTEND_HOST_PORT` (default 3000) are free and automatically walks forward to the next free port for whichever one is busy, then runs `docker compose up` with those ports. Any extra flags (e.g. `-d`) are passed through: `pnpm docker:up -d`.

## Learn More

To learn more, take a look at the following resources:

- [Next.js Documentation](https://nextjs.org/docs) - learn about Next.js features and API.
- [Learn Next.js](https://nextjs.org/learn) - an interactive Next.js tutorial.
- [v0 Documentation](https://v0.app/docs) - learn about v0 and how to use it.
