#!/bin/sh
# Dockerfile.allinone's ENTRYPOINT. Runs as root, once, before supervisord
# takes over -- same shape as backend/scripts/container-entrypoint.sh (that
# script's own Postgres bootstrap logic, adapted here for a loopback-only
# Postgres since nothing outside this container ever talks to it directly),
# plus rendering nginx's listen port from whatever $PORT the host platform
# injects (Render, Railway, Fly, a plain `docker run -p`, etc. each pick
# their own convention; templating it here means this same image needs no
# platform-specific Dockerfile or config -- just "deploy this image").
set -e

PGBIN=/usr/lib/postgresql/16/bin
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
POSTGRES_USER="${POSTGRES_USER:-tradingos}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-tradingos}"
POSTGRES_DB="${POSTGRES_DB:-tradingos}"

mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[entrypoint] Initializing Postgres data directory at $PGDATA"
    su postgres -c "$PGBIN/initdb -D $PGDATA --username=postgres --auth=trust"

    # Loopback-only: unlike backend/Dockerfile's multi-container use, this
    # image never needs Postgres reachable from outside itself -- only the
    # sibling API process in this same container connects, over localhost.
    echo "listen_addresses = 'localhost'" >> "$PGDATA/postgresql.conf"

    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-c listen_addresses=localhost' -w start"

    cat > /tmp/tradingos-init.sql <<SQL
CREATE ROLE ${POSTGRES_USER} WITH LOGIN PASSWORD '${POSTGRES_PASSWORD}' CREATEDB;
CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};
SQL
    chmod 644 /tmp/tradingos-init.sql
    su postgres -c "psql -v ON_ERROR_STOP=1 --username postgres -f /tmp/tradingos-init.sql"
    rm -f /tmp/tradingos-init.sql

    su postgres -c "$PGBIN/pg_ctl -D $PGDATA -m fast -w stop"
    echo "[entrypoint] Postgres initialized."
else
    echo "[entrypoint] Postgres data directory already initialized, skipping bootstrap."
fi

mkdir -p /var/lib/redis
chown -R redis:redis /var/lib/redis

export PORT="${PORT:-8080}"
echo "[entrypoint] Rendering nginx config for PORT=${PORT}"
envsubst '${PORT}' < /etc/nginx/conf.d/tradingos.conf.template > /etc/nginx/conf.d/tradingos.conf

exec supervisord -n -c /etc/supervisor/conf.d/tradingos.conf
