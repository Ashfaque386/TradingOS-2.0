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

# The API runs as the unprivileged `tradingos` user, but the volumes it writes to may have been filled
# by an earlier version that ran as root (or be fresh and root-owned): hand each over once. The top
# directory's owner says whether that has been done, so a restart does not walk a large data lake again.
for dir in /app/secrets /app/data_lake /app/data_lake_backups /app/audit_archive /app/backups \
        /app/alertmanager_token /app/config; do
    mkdir -p "$dir"
    if [ "$(stat -c %U "$dir")" != "tradingos" ]; then
        echo "[entrypoint] Giving $dir to the tradingos user"
        chown -R tradingos:tradingos "$dir" \
            || echo "[entrypoint] WARNING: could not change the owner of $dir; the API may not be able to write there"
    fi
done

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
