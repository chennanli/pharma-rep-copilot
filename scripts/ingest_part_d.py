"""
Ingest CMS Medicare Part D Prescriber by Provider and Drug into Postgres.

Input : a Part D Prescriber-by-Provider-and-Drug CSV downloaded from CMS.
        Expected at: data/raw/medicare_part_d_prescriber_by_provider_and_drug.csv
        (rename whatever you downloaded to this name.)

Output: rows in partd.prescriber_drug_yearly, filtered to ONE STATE (default CA).

CMS column names change occasionally. This script normalizes the most recent
CMS naming (RY24, data year 2022). If the schema differs, edit COL_MAP below.

Streaming — reads the file row-by-row, filters, and COPYs into Postgres in batches.
Doesn't load the whole CSV into memory.

Usage:
    python scripts/ingest_part_d.py [--state CA] [--year 2022]
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DEFAULT_INPUT = ROOT / "data" / "raw" / "medicare_part_d_prescriber_by_provider_and_drug.csv"

# CMS Medicare Part D Prescriber by Provider and Drug — RY24 (data year 2022) column names.
# Update if your file uses different headers.
COL_MAP = {
    "Prscrbr_NPI":           "prscrbr_npi",
    "Prscrbr_Last_Org_Name": "prscrbr_last",
    "Prscrbr_First_Name":    "prscrbr_first",
    "Prscrbr_City":          "prscrbr_city",
    "Prscrbr_State_Abrvtn":  "prscrbr_state",
    "Prscrbr_Type":          "prscrbr_type",
    "Brnd_Name":             "brnd_name",
    "Gnrc_Name":             "gnrc_name",
    "Tot_Clms":              "tot_clms",
    "Tot_30day_Fills":       "tot_30day_fills",
    "Tot_Day_Suply":         "tot_day_suply",
    "Tot_Drug_Cst":          "tot_drug_cst",
}


def to_int(v):
    if v in (None, "", "*"):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def to_num(v):
    if v in (None, "", "*"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--state", default="CA")
    p.add_argument("--year", type=int, default=2022,
                   help="Data year — stored in the row, not used for filtering (CMS file is single-year).")
    p.add_argument("--batch", type=int, default=2000)
    args = p.parse_args()

    inpath = Path(args.input)
    if not inpath.exists():
        print(f"✗ Input file not found: {inpath}")
        print("  Download Part D Prescribers by Provider and Drug from:")
        print("  https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers/medicare-part-d-prescribers-by-provider-and-drug")
        print(f"  Rename and place at: {inpath}")
        sys.exit(2)

    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    db = os.getenv("PG_DB", "hcp_insights")
    user = os.getenv("PG_ADMIN_USER", "hcp_admin")
    pw = os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only")

    print(f"▶ Streaming {inpath.name}, filtering state='{args.state}' ...")
    inserted = 0
    skipped = 0
    batch: list[tuple] = []

    cols_out = ["prscrbr_npi", "prscrbr_last", "prscrbr_first", "prscrbr_city",
                "prscrbr_state", "prscrbr_type", "brnd_name", "gnrc_name", "year",
                "tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst"]
    placeholders = ",".join(["%s"] * len(cols_out))
    insert_sql = (f"INSERT INTO partd.prescriber_drug_yearly ({','.join(cols_out)}) "
                  f"VALUES ({placeholders})")

    with psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE partd.prescriber_drug_yearly")
        conn.commit()

        with conn.cursor() as cur, open(inpath, encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            # Resolve real header names (case-insensitive)
            header_norm = {h.lower(): h for h in (reader.fieldnames or [])}
            def col(key):
                return header_norm.get(key.lower())

            for i, row in enumerate(reader, 1):
                state_col = col("Prscrbr_State_Abrvtn")
                if not state_col or row.get(state_col, "").strip().upper() != args.state.upper():
                    skipped += 1
                    continue

                def g(k, row=row):  # bind row at definition to avoid late-binding closure
                    real = col(k)
                    return (row.get(real) if real else None)

                rec = (
                    (g("Prscrbr_NPI") or "").strip(),
                    (g("Prscrbr_Last_Org_Name") or "").strip(),
                    (g("Prscrbr_First_Name") or "").strip(),
                    (g("Prscrbr_City") or "").strip(),
                    (g("Prscrbr_State_Abrvtn") or "").strip().upper(),
                    (g("Prscrbr_Type") or "").strip(),
                    (g("Brnd_Name") or "").strip().upper() or None,
                    (g("Gnrc_Name") or "").strip().upper() or None,
                    args.year,
                    to_int(g("Tot_Clms")),
                    to_num(g("Tot_30day_Fills")),
                    to_int(g("Tot_Day_Suply")),
                    to_num(g("Tot_Drug_Cst")),
                )
                batch.append(rec)
                if len(batch) >= args.batch:
                    cur.executemany(insert_sql, batch)
                    inserted += len(batch)
                    batch.clear()
                    if inserted % 50_000 == 0:
                        print(f"  ... {inserted:,} rows inserted")
                        conn.commit()

            if batch:
                cur.executemany(insert_sql, batch)
                inserted += len(batch)
        conn.commit()

    print(f"✓ partd.prescriber_drug_yearly: inserted {inserted:,} CA rows, skipped {skipped:,} non-CA")


if __name__ == "__main__":
    main()
