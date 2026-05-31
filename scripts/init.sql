-- HCP-Insights — initial schema, tables, and read-only role.
-- Applied by scripts/entrypoint.sh (Docker) or scripts/setup_postgres.sh (local).
-- Idempotent: every CREATE uses IF NOT EXISTS; role creation is guarded.

-- ====== Schemas ======
CREATE SCHEMA IF NOT EXISTS payments;
CREATE SCHEMA IF NOT EXISTS partd;
CREATE SCHEMA IF NOT EXISTS npi;
CREATE SCHEMA IF NOT EXISTS reference;

-- ====== Read-only role ======
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hcp_agent_ro') THEN
    CREATE ROLE hcp_agent_ro LOGIN PASSWORD 'readonly-dev-only';
  END IF;
END$$;

GRANT USAGE ON SCHEMA payments, partd, npi, reference TO hcp_agent_ro;
ALTER ROLE hcp_agent_ro SET statement_timeout = '30s';
ALTER ROLE hcp_agent_ro SET idle_in_transaction_session_timeout = '60s';

-- ====== Warehouse tables ======

-- CMS Open Payments — General Payments
CREATE TABLE IF NOT EXISTS payments.general_payments (
  record_id                   BIGINT PRIMARY KEY,
  program_year                INT,
  payment_publication_date    DATE,
  covered_recipient_npi       TEXT,
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
  prscrbr_type   TEXT,
  brnd_name      TEXT,
  gnrc_name      TEXT,
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

-- Derived NPI registry
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

-- Brand-to-generic lookup
CREATE TABLE IF NOT EXISTS reference.drug_alias (
  brand   TEXT,
  generic TEXT,
  notes   TEXT,
  PRIMARY KEY (brand, generic)
);

-- ====== Grants ======
GRANT SELECT ON ALL TABLES IN SCHEMA payments, partd, npi, reference TO hcp_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA payments  GRANT SELECT ON TABLES TO hcp_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA partd     GRANT SELECT ON TABLES TO hcp_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA npi       GRANT SELECT ON TABLES TO hcp_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA reference GRANT SELECT ON TABLES TO hcp_agent_ro;
