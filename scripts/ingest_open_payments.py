"""
Ingest CMS Open Payments — General Payments — into Postgres.

Input : OP_DTL_GNRL_PGYR{YYYY}_*.csv extracted from the CMS yearly ZIP.
        Expected at: data/raw/open_payments_general.csv
        (rename whatever you downloaded to this name.)

Output: rows in payments.general_payments, filtered to ONE STATE (default CA).

Streaming — reads the file row-by-row to keep memory bounded.

Usage:
    python scripts/ingest_open_payments.py [--state CA]
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

DEFAULT_INPUT = ROOT / "data" / "raw" / "open_payments_general.csv"

# Columns we keep from the CMS file. Names are stable for PGYR2018+.
WANTED = {
    "Record_ID":                                   "record_id",
    "Program_Year":                                "program_year",
    "Payment_Publication_Date":                    "payment_publication_date",
    "Covered_Recipient_NPI":                       "covered_recipient_npi",
    "Covered_Recipient_First_Name":                "recipient_first_name",
    "Covered_Recipient_Last_Name":                 "recipient_last_name",
    "Recipient_City":                              "recipient_city",
    "Recipient_State":                             "recipient_state",
    "Covered_Recipient_Specialty_1":               "recipient_specialty",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name": "applicable_manufacturer",
    "Total_Amount_of_Payment_USDollars":           "total_amount_of_payment_usdollars",
    "Nature_of_Payment_or_Transfer_of_Value":      "nature_of_payment",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1": "product_name",
    "Product_Category_or_Therapeutic_Area_1":      "product_indication",
}


def parse_date(v):
    if not v:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            from datetime import datetime
            return datetime.strptime(v.strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_int(v):
    if v in (None, ""):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def parse_num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(DEFAULT_INPUT))
    p.add_argument("--state", default="CA")
    p.add_argument("--batch", type=int, default=2000)
    args = p.parse_args()

    inpath = Path(args.input)
    if not inpath.exists():
        print(f"✗ Input file not found: {inpath}")
        print("  Download CMS Open Payments yearly ZIP from:")
        print("  https://www.cms.gov/openpayments/data/datasetdownloads")
        print("  Unzip; copy OP_DTL_GNRL_PGYR{YYYY}_*.csv to:")
        print(f"    {inpath}")
        sys.exit(2)

    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    db = os.getenv("PG_DB", "hcp_insights")
    user = os.getenv("PG_ADMIN_USER", "hcp_admin")
    pw = os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only")

    print(f"▶ Streaming {inpath.name}, filtering Recipient_State='{args.state}' ...")
    inserted = 0
    skipped = 0
    batch: list[tuple] = []
    cols_out = list(WANTED.values())
    placeholders = ",".join(["%s"] * len(cols_out))
    insert_sql = (f"INSERT INTO payments.general_payments ({','.join(cols_out)}) "
                  f"VALUES ({placeholders}) ON CONFLICT (record_id) DO NOTHING")

    with psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE payments.general_payments")
        conn.commit()

        with conn.cursor() as cur, open(inpath, encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh)
            header_norm = {h.lower(): h for h in (reader.fieldnames or [])}
            def col(key):
                return header_norm.get(key.lower())

            for row in reader:
                st_col = col("Recipient_State")
                if not st_col or row.get(st_col, "").strip().upper() != args.state.upper():
                    skipped += 1
                    continue

                def g(k, row=row):  # bind row at definition to avoid late-binding closure
                    real = col(k)
                    return row.get(real) if real else None

                rec = (
                    parse_int(g("Record_ID")),
                    parse_int(g("Program_Year")),
                    parse_date(g("Payment_Publication_Date")),
                    (g("Covered_Recipient_NPI") or "").strip(),
                    (g("Covered_Recipient_First_Name") or "").strip(),
                    (g("Covered_Recipient_Last_Name") or "").strip(),
                    (g("Recipient_City") or "").strip(),
                    (g("Recipient_State") or "").strip().upper(),
                    (g("Covered_Recipient_Specialty_1") or "").strip(),
                    (g("Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name") or "").strip(),
                    parse_num(g("Total_Amount_of_Payment_USDollars")),
                    (g("Nature_of_Payment_or_Transfer_of_Value") or "").strip(),
                    (g("Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1") or "").strip(),
                    (g("Product_Category_or_Therapeutic_Area_1") or "").strip(),
                )
                if rec[0] is None:
                    skipped += 1
                    continue
                batch.append(rec)
                if len(batch) >= args.batch:
                    cur.executemany(insert_sql, batch)
                    inserted += len(batch)
                    batch.clear()
                    if inserted % 50_000 == 0:
                        print(f"  ... {inserted:,} inserted")
                        conn.commit()

            if batch:
                cur.executemany(insert_sql, batch)
                inserted += len(batch)
        conn.commit()

    print(f"✓ payments.general_payments: inserted {inserted:,} CA rows, skipped {skipped:,}")


if __name__ == "__main__":
    main()
