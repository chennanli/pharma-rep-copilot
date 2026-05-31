"""
Build npi.npi_registry from the distinct NPIs already loaded in partd.prescriber_drug_yearly.

Saves us from having to download the 7GB NPPES Monthly file just for ~250K CA HCPs.
Specialty + first/last name + city + state all come from Part D rows.

Idempotent: TRUNCATEs and re-derives.

Usage:
    python scripts/derive_npi_registry.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


SQL_DERIVE = """
TRUNCATE npi.npi_registry;
INSERT INTO npi.npi_registry (npi, first_name, last_name, city, state, specialty)
SELECT
  prscrbr_npi   AS npi,
  -- For HCPs with multiple Part D rows, MAX is arbitrary but stable.
  MAX(prscrbr_first) AS first_name,
  MAX(prscrbr_last)  AS last_name,
  MAX(prscrbr_city)  AS city,
  MAX(prscrbr_state) AS state,
  MAX(prscrbr_type)  AS specialty
FROM partd.prescriber_drug_yearly
WHERE prscrbr_npi IS NOT NULL AND prscrbr_npi <> ''
GROUP BY prscrbr_npi;

ANALYZE npi.npi_registry;
"""


def main():
    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    db = os.getenv("PG_DB", "hcp_insights")
    user = os.getenv("PG_ADMIN_USER", "hcp_admin")
    pw = os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only")

    print("▶ Deriving npi.npi_registry from Part D distinct NPIs ...")
    with psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw) as conn:
        with conn.cursor() as cur:
            cur.execute(SQL_DERIVE)
            cur.execute("SELECT COUNT(*) FROM npi.npi_registry")
            (n,) = cur.fetchone()
        conn.commit()
        print(f"✓ npi.npi_registry now has {n:,} HCPs.")


if __name__ == "__main__":
    main()
