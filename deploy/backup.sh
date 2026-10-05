#!/bin/sh
# Periodic PostgreSQL backup for the compose "backup" service (postgres image; password read from the secret file).
set -eu

: "${PGHOST:?PGHOST is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGDATABASE:?PGDATABASE is required}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP:-14}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"

PGPASSWORD="$(cat /run/secrets/db_password)"
export PGPASSWORD

while true; do
    stamp="$(date -u +%Y%m%dT%H%M%SZ)"
    target="${BACKUP_DIR}/trading-${stamp}.dump"
    if pg_dump --format=custom --no-owner -f "${target}.partial" "${PGDATABASE}"; then
        mv "${target}.partial" "${target}"
        echo "backup written: ${target}"
        # keep only the newest $KEEP dumps
        ls -1t "${BACKUP_DIR}"/trading-*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
            rm -f -- "${old}"
        done
    else
        rm -f "${target}.partial"
        echo "backup FAILED at ${stamp}" >&2
    fi
    sleep "${INTERVAL}"
done
