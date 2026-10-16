# Single-container image: Postgres 16 and the FastAPI app run as two
# supervisord-managed processes inside one container, rather than as
# separate containers. See docker-compose.yml at the repo root (one
# service) and backend/supervisord.conf for the process definitions.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PGDATA=/var/lib/postgresql/data \
    PATH="/usr/lib/postgresql/16/bin:${PATH}"

# Postgres 16 via the PGDG apt repo (Debian bookworm ships PG15 by default).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg lsb-release supervisor \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc --fail \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

# EXTRA_CA_PEM (docker-compose.yml's `extra_ca` secret) is optional: with it, pip also trusts a
# TLS-intercepting proxy's root; without it this is the plain `pip install .`. Not kept in the image.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then \
        cat "$(python -c 'import pip._vendor.certifi as c; print(c.where())')" /run/secrets/extra_ca \
            > /tmp/ca-bundle.pem \
        && export PIP_CERT=/tmp/ca-bundle.pem; \
    fi \
    && pip install . \
    && rm -f /tmp/ca-bundle.pem \
    && sed -i 's/\r$//' scripts/container-entrypoint.sh scripts/api-entrypoint.sh \
    && chmod +x scripts/container-entrypoint.sh scripts/api-entrypoint.sh

COPY supervisord.conf /etc/supervisor/conf.d/tradingos.conf

RUN mkdir -p /var/log/supervisor "$PGDATA" \
    && chown -R postgres:postgres "$PGDATA"

EXPOSE 8000 5432

HEALTHCHECK --interval=10s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/scripts/container-entrypoint.sh"]
