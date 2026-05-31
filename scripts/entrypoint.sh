#!/usr/bin/env bash
# App container entrypoint.
#
# 1. Wait for Postgres to accept connections.
# 2. If FIRST_RUN sentinel is missing in /app/data, apply DDL + synthetic seed.
# 3. exec the supplied CMD (uvicorn by default).
set -euo pipefail

PGHOST=${PG_HOST:-postgres}
PGPORT=${PG_PORT:-5432}
PGADMIN=${PG_ADMIN_USER:-hcp_admin}
PGADMINPW=${PG_ADMIN_PASSWORD:-admin-dev-only}
PGDB=${PG_DB:-hcp_insights}
SENTINEL=/app/data/.bootstrap_done
AUTO_SEED=${AUTO_SEED:-synth}   # synth | none

echo "▶ Waiting for Postgres at ${PGHOST}:${PGPORT} ..."
for i in $(seq 1 60); do
  if PGPASSWORD="${PGADMINPW}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGADMIN}" -d "${PGDB}" -c "SELECT 1" >/dev/null 2>&1; then
    echo "✓ Postgres ready"
    break
  fi
  sleep 1
done

mkdir -p /app/data

if [ ! -f "${SENTINEL}" ]; then
  echo "▶ First-run bootstrap: applying DDL ..."
  PGPASSWORD="${PGADMINPW}" psql -h "${PGHOST}" -p "${PGPORT}" -U "${PGADMIN}" -d "${PGDB}" \
    -v ON_ERROR_STOP=1 -f /app/scripts/init.sql || true

  if [ "${AUTO_SEED}" = "synth" ]; then
    echo "▶ First-run bootstrap: loading synthetic seed ..."
    python /app/scripts/synth_seed.py
    python /app/scripts/seed_drug_alias.py
  else
    echo "▶ AUTO_SEED=${AUTO_SEED} — skipping seed; run 'make seed-real' or seed manually."
  fi

  touch "${SENTINEL}"
  echo "✓ Bootstrap complete"
else
  echo "✓ Bootstrap sentinel present — skipping init."
fi

exec "$@"
