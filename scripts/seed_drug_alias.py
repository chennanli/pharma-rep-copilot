"""
Seed the reference.drug_alias table with a small set of pharma-commercial-relevant
brand → generic pairs.

This is intentionally hand-curated and small. The agent uses this table to bridge
brand names (Herceptin) to generic names (TRASTUZUMAB) when filtering Part D,
which stores generic names. The "learning moment" demo relies on the agent
discovering this pattern via a verified example — but the table itself must
already be seeded so the corrected SQL can actually JOIN through it.

Idempotent: existing rows are skipped.

Usage:
    python scripts/seed_drug_alias.py
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


# (brand_uppercase, generic_uppercase, optional_note)
# All values stored uppercase because Part D's gnrc_name is uppercase.
ALIASES = [
    # HER2+ breast oncology (the demo arc)
    ("HERCEPTIN",  "TRASTUZUMAB",                "Roche; HER2+ breast cancer first-line"),
    ("KADCYLA",    "ADO-TRASTUZUMAB EMTANSINE",  "Roche; HER2+ second-line ADC"),
    ("PERJETA",    "PERTUZUMAB",                 "Roche; HER2+ combo"),
    ("PHESGO",     "PERTUZUMAB/TRASTUZUMAB/HYALURONIDASE-ZZXF", "Roche; subQ combo"),

    # Other oncology blockbusters analysts ask about
    ("KEYTRUDA",   "PEMBROLIZUMAB",              "Merck; anti-PD-1"),
    ("OPDIVO",     "NIVOLUMAB",                  "BMS; anti-PD-1"),
    ("TECENTRIQ",  "ATEZOLIZUMAB",               "Roche; anti-PD-L1"),

    # GLP-1 / endocrinology
    ("OZEMPIC",    "SEMAGLUTIDE",                "Novo Nordisk; injectable GLP-1"),
    ("MOUNJARO",   "TIRZEPATIDE",                "Eli Lilly; GIP/GLP-1"),
    ("RYBELSUS",   "SEMAGLUTIDE",                "Novo Nordisk; oral GLP-1"),

    # Autoimmune
    ("HUMIRA",     "ADALIMUMAB",                 "AbbVie; TNF inhibitor"),
    ("ENBREL",     "ETANERCEPT",                 "Amgen; TNF inhibitor"),
    ("STELARA",    "USTEKINUMAB",                "Janssen; IL-12/23"),

    # Cardio / ARNI
    ("ENTRESTO",   "SACUBITRIL/VALSARTAN",       "Novartis; ARNI for HF"),

    # Statins (legacy)
    ("LIPITOR",    "ATORVASTATIN",               "Pfizer; statin"),
    ("CRESTOR",    "ROSUVASTATIN",               "AstraZeneca; statin"),
]


def main():
    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    db = os.getenv("PG_DB", "hcp_insights")
    user = os.getenv("PG_ADMIN_USER", "hcp_admin")
    pw = os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only")

    print(f"▶ Seeding reference.drug_alias on {host}:{port}/{db} ...")
    with psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO reference.drug_alias (brand, generic, notes) "
                "VALUES (%s, %s, %s) ON CONFLICT (brand, generic) DO NOTHING",
                ALIASES,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM reference.drug_alias")
            (n,) = cur.fetchone()
            print(f"✓ reference.drug_alias now has {n} rows ({len(ALIASES)} attempted).")


if __name__ == "__main__":
    main()
