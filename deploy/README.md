# All-in-one deployment image

`Dockerfile.allinone` (repo root) bundles the frontend, backend, Postgres,
Redis, and an nginx reverse proxy into **one image, one container, one
public port**. It exists specifically so a single hosting platform service
— Render, Railway, Fly, a bare `docker run`, whatever — can deploy this app
with no per-platform wiring: no separate database/Redis service to
provision, no `NEXT_PUBLIC_API_URL`/`NEXT_PUBLIC_WS_URL` build args to get
right, no `CORS_ORIGINS` to keep in sync with wherever the platform
publishes the service. The frontend and backend are served from the same
origin (nginx routes `/api/*` and `/health` to the backend, everything else
to the frontend), so the browser never makes a cross-origin request in the
first place.

This is separate from the root `Dockerfile` (frontend only) and
`backend/Dockerfile` (backend + its own embedded Postgres) used by
`docker-compose.yml` for local multi-service dev — those are unchanged and
still the right choice for local iteration. Use `Dockerfile.allinone` for a
single-service deployment instead.

## Deploying to Render

1. Create one **Web Service**, Docker runtime, pointed at this repo.
2. In the service's Settings, set **Dockerfile Path** to `Dockerfile.allinone`
   (Root Directory stays the repo root).
3. That's it for a first working deploy — no environment variables are
   required. Render sets `PORT` itself; the image's nginx listens on
   whatever it's given.
4. Optional environment variables, worth setting for anything beyond a
   quick test:
   - `JWT_SECRET_KEY` — defaults to a placeholder value; generate a real
     one (`python -c "import secrets; print(secrets.token_urlsafe(48))"`).
   - `DEFAULT_ADMIN_EMAIL` + `DEFAULT_ADMIN_PASSWORD` — set both to have
     the container ensure that `SystemAdministrator` user exists on every
     boot. Unset by default; without it, register the first user through
     the app's own `/register` page instead (the first registered user on
     a fresh system automatically becomes `SystemAdministrator`).
   - `SECRETS_ENCRYPTION_KEY` — needed only for the broker-credentials
     feature (live trading). Generate with
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## The persistence trade-off

Postgres's and Redis's data live on this container's own filesystem, the
same design `backend/Dockerfile` already uses for local/self-hosted
Docker. Most platforms' default "ephemeral" instances do **not** persist
that across a restart or redeploy — on Render specifically, a free-tier
instance spins down after inactivity and loses it on the next wake-up.

- Redis's data here is disposable by nature (pub/sub, rate-limit counters,
  the mock tick stream) and losing it on restart is harmless.
- Postgres's data (users, strategies, orders, the audit log) is not. If you
  need it to survive restarts, attach a persistent disk/volume mounted at
  `/var/lib/postgresql/data` (on Render, this needs a paid instance type —
  Disks aren't available on the free tier). Without one, treat this
  deployment as good for testing the app, not for anything you need to
  keep.

## Verifying it locally without Docker

If you want to sanity-check the same shape this image runs without a
Docker daemon available (this is exactly how it was validated while
building it):

```
NEXT_PUBLIC_API_URL="" NEXT_PUBLIC_WS_URL="" pnpm build
cp -r .next/static .next/standalone/.next/static
cp -r public .next/standalone/public
PORT=3000 HOSTNAME=127.0.0.1 node .next/standalone/server.js &

cd backend && UVICORN_HOST=127.0.0.1 .venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8000 &

PORT=8091 envsubst '${PORT}' < ../deploy/nginx.conf.template > /etc/nginx/conf.d/tradingos.conf
nginx -g 'daemon off;' &

curl http://127.0.0.1:8091/health
```
