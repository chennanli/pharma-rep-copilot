#!/usr/bin/env bash
# One-time setup: create schemas, read-only role, and warehouse tables.
#
# Prerequisite: Postgres is running (e.g. `docker compose up -d`).
#
# Idempotent — safe to re-run. Creates schemas if missing, role if missing.
#
# Schemas created:
#   payments  — CMS Open Payments general payments
#   partd     — Medicare Part D Prescriber by Provider and Drug
#   npi       — derived NPI registry (one row per distinct NPI from Part D)
#   reference — small lookup tables (drug_alias)
#
# Roles:
#   hcp_admin     — superuser used by ingest scripts (created by Docker image init)
#   hcp_agent_ro  — read-only role used by the agent (created here)
set -euo pipefail

CONTAINER=${CONTAINER:-hcp-pg}
DB=${PG_DB:-hcp_insights}
ADMIN_USER=${PG_ADMIN_USER:-hcp_admin}
RO_USER=${PG_USER:-hcp_agent_ro}
RO_PW=${PG_PASSWORD:-readonly-dev-only}

echo "▶ Waiting for Postgres in container '${CONTAINER}'..."
for i in {1..30}; do
  if docker exec "${CONTAINER}" pg_isready -U "${ADMIN_USER}" -d "${DB}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "▶ Creating schemas, read-only role, and warehouse tables..."

docker exec -i "${CONTAINER}" psql -U "${ADMIN_USER}" -d "${DB}" <<SQL
-- ====== Schemas ======
CREATE SCHEMA IF NOT EXISTS payments;
CREATE SCHEMA IF NOT EXISTS partd;
CREATE SCHEMA IF NOT EXISTS npi;
CREATE SCHEMA IF NOT EXISTS reference;

-- ====== Read-only role ======
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${RO_USER}') THEN
    CREATE ROLE ${RO_USER} LOGIN PASSWORD '${RO_PW}';
  END IF;
END\$\$;

GRANT USAGE ON SCHEMA payments, partd, npi, reference TO ${RO_USER};
ALTER ROLE ${RO_USER} SET statement_timeout = '30s';
ALTER ROLE ${RO_USER} SET idle_in_transaction_session_timeout = '60s';

-- ====== Warehouse tables ======

-- CMS Open Payments — General Payments (one row per industry-to-HCP payment)
CREATE TABLE IF NOT EXISTS payments.general_payments (
  record_id                   BIGINT PRIMARY KEY,
  program_year                INT,
  payment_publication_date    DATE,
  covered_recipient_npi       TEXT,           -- 10-digit NPI
  recipient_first_name        TEXT,
  recipient_last_name         TEXT,
  recipient_city              TEXT,
  recipient_state             TEXT,
  recipient_specialty         TEXT,
  applicable_manufacturer     TEXT,
  total_amount_of_payment_usdollars NUMERIC(14,2),
  nature_of_payment           TEXT,
  product_name                TEXT,
  product_indication          TEXT
);
CREATE INDEX IF NOT EXISTS gp_state_idx ON payments.general_payments(recipient_state);
CREATE INDEX IF NOT EXISTS gp_npi_idx   ON payments.general_payments(covered_recipient_npi);

-- Medicare Part D Prescriber by Provider and Drug
CREATE TABLE IF NOT EXISTS partd.prescriber_drug_yearly (
  prscrbr_npi    TEXT,
  prscrbr_last   TEXT,
  prscrbr_first  TEXT,
  prscrbr_city   TEXT,
  prscrbr_state  TEXT,
  prscrbr_type   TEXT,            -- "Oncology", "Endocrinology", etc.
  brnd_name      TEXT,            -- e.g. HERCEPTIN (often NULL — CMS uses generic-only sometimes)
  gnrc_name      TEXT,            -- e.g. TRASTUZUMAB
  year           INT,
  tot_clms       INT,
  tot_30day_fills NUMERIC(14,2),
  tot_day_suply  INT,
  tot_drug_cst   NUMERIC(14,2)
);
CREATE INDEX IF NOT EXISTS pd_state_idx  ON partd.prescriber_drug_yearly(prscrbr_state);
CREATE INDEX IF NOT EXISTS pd_npi_idx    ON partd.prescriber_drug_yearly(prscrbr_npi);
CREATE INDEX IF NOT EXISTS pd_brnd_idx   ON partd.prescriber_drug_yearly(brnd_name);
CREATE INDEX IF NOT EXISTS pd_gnrc_idx   ON partd.prescriber_drug_yearly(gnrc_name);

-- NPI Registry (derived from Part D distinct NPIs — see scripts/derive_npi.py)
CREATE TABLE IF NOT EXISTS npi.npi_registry (
  npi          TEXT PRIMARY KEY,
  first_name   TEXT,
  last_name    TEXT,
  city         TEXT,
  state        TEXT,
  specialty    TEXT
);
CREATE INDEX IF NOT EXISTS npi_state_idx     ON npi.npi_registry(state);
CREATE INDEX IF NOT EXISTS npi_specialty_idx ON npi.npi_registry(specialty);

-- Brand <-> generic lookup. Seeded by scripts/seed_drug_alias.py.
-- The agent should JOIN this when a question mentions a brand name and Part D
-- only stores generic names.
CREATE TABLE IF NOT EXISTS reference.drug_alias (
  brand   TEXT,
  generic TEXT,
  notes   TEXT,
  PRIMARY KEY (brand, generic)
);
COMMENT ON TABLE reference.drug_alias IS 'Brand-to-generic lookup. JOIN this when filtering Part D by brand name.';

-- ====== Grant SELECT on the new tables to the read-only role ======
GRANT SELECT ON ALL TABLES IN SCHEMA payments, partd, npi, reference TO ${RO_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA payments  GRANT SELECT ON TABLES TO ${RO_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA partd     GRANT SELECT ON TABLES TO ${RO_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA npi       GRANT SELECT ON TABLES TO ${RO_USER};
ALTER DEFAULT PRIVILEGES IN SCHEMA reference GRANT SELECT ON TABLES TO ${RO_USER};
SQL

echo
echo "✓ Schemas, tables and read-only role ready."
echo
echo "Next step: bash scripts/setup_data.sh"
