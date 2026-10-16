#!/bin/sh
# Container ENTRYPOINT. Runs as root, once, before supervisord takes over.
#
# Postgres and the API run as two processes inside this one container
# (supervisord-managed) rather than as separate containers. On first boot
# (empty PGDATA volume) this bootstraps a Postgres cluster and creates the
# app role/database by briefly starting Postgres standalone, then hands off
# to supervisord for the long-running postgres + api processes.
set -e

PGBIN=/usr/lib/postgresql/16/bin
PGDATA="${PGDATA:-/var/lib/postgresql/data}"
POSTGRES_USER="${POSTGRES_USER:-tradingos}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-tradingos}"
POSTGRES_DB="${POSTGRES_DB:-tradingos}"

# Optional: trust one extra root certificate for the API's outbound HTTPS (broker, LLM, Telegram...),
# e.g. a TLS-intercepting antivirus/proxy on the host (Norton Web/Mail Shield) that re-signs those
# sites with its own root -- without it every call fails "CERTIFICATE_VERIFY_FAILED" and Settings
# shows "could not reach <broker>". EXTRA_CA_PEM is the PEM text (scripts/docker-up.js fills it from
# .local/extra-ca.pem). The bundle is the normal public roots plus that one; unset, nothing changes.
if [ -n "${EXTRA_CA_PEM:-}" ]; then
    CA_BUNDLE=/etc/ssl/tradingos-ca-bundle.pem
    {
        cat "$(python -c 'import certifi; print(certifi.where())')"
        printf '\n%s\n' "$EXTRA_CA_PEM"
    } > "$CA_BUNDLE"
    export SSL_CERT_FILE="$CA_BUNDLE" REQUESTS_CA_BUNDLE="$CA_BUNDLE" CURL_CA_BUNDLE="$CA_BUNDLE"
    echo "[entrypoint] Outbound HTTPS also trusts the certificate in EXTRA_CA_PEM."
fi

mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[entrypoint] Initializing Postgres data directory at $PGDATA"
    su postgres -c "$PGBIN/initdb -D $PGDATA --username=postgres --auth=trust"

    echo "listen_addresses = '*'" >> "$PGDATA/postgresql.conf"
    echo "host all all 0.0.0.0/0 scram-sha-256" >> "$PGDATA/pg_hba.conf"

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

exec supervisord -n -c /etc/supervisor/conf.d/tradingos.conf
