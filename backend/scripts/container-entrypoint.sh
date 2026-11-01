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
# .local/extra-ca.pem). It is also read straight from /extra-ca/extra-ca.pem, the repo's .local/
# directory mounted by docker-compose.yml, so a stack started any way at all (plain `docker compose
# up`, Docker Desktop's start button) trusts it, not only one started by that script. The bundle is
# the normal public roots plus that one; neither present, nothing changes.
EXTRA_CA_FILE_IN_IMAGE=/extra-ca/extra-ca.pem
if [ -z "${EXTRA_CA_PEM:-}" ] && [ -s "$EXTRA_CA_FILE_IN_IMAGE" ] \
        && grep -q 'BEGIN CERTIFICATE' "$EXTRA_CA_FILE_IN_IMAGE"; then
    EXTRA_CA_PEM="$(cat "$EXTRA_CA_FILE_IN_IMAGE")"
fi
if [ -n "${EXTRA_CA_PEM:-}" ]; then
    CA_BUNDLE=/etc/ssl/tradingos-ca-bundle.pem
    {
        cat "$(python -c 'import certifi; print(certifi.where())')"
        printf '\n%s\n' "$EXTRA_CA_PEM"
    } > "$CA_BUNDLE"
    export SSL_CERT_FILE="$CA_BUNDLE" REQUESTS_CA_BUNDLE="$CA_BUNDLE" CURL_CA_BUNDLE="$CA_BUNDLE"
    echo "[entrypoint] Outbound HTTPS also trusts the extra certificate (EXTRA_CA_PEM or /extra-ca/extra-ca.pem)."
fi

# The API runs as the unprivileged `tradingos` user, but the volumes it writes to may have been filled
# by an earlier version that ran as root (or be fresh and root-owned): hand each over once. The top
# directory's owner says whether that has been done, so a restart does not walk a large data lake again.
for dir in /app/secrets /app/data_lake /app/data_lake_backups /app/audit_archive /app/backups \
        /app/alertmanager_token; do
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
