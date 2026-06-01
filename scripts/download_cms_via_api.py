"""
Download CMS Medicare Part D + Open Payments via the CMS Data API,
filtered to one state, paginated. No browser needed.

This script targets the CMS Data API REST endpoints:
  https://data.cms.gov/data-api/v1/dataset/{dataset_uuid}/data

CMS rotates dataset UUIDs once per year. The constants below point at the
most recent releases known as of 2026-05. If a 404 comes back the script
prints the discovery URL so you can grab the current UUID.

This is the "real data" path. For a fast demo, use `python scripts/synth_seed.py`
instead — no download required.

Usage:
    python scripts/download_cms_via_api.py --state CA
    python scripts/download_cms_via_api.py --state CA --max-rows 50000

Output:
    data/raw/part_d_via_api.csv
    data/raw/open_payments_via_api.csv
    (then loaded into Postgres via the same logic as ingest_*.py)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


# CMS Data API dataset UUIDs (rotate ~yearly).
# Discovery page (paste into browser if 404):
#   https://data.cms.gov/provider-summary-by-type-of-service
PART_D_DATASETS = [
    # Try in order — newest first. UUIDs rotate ~yearly; verified current 2026-05.
    ("Medicare Part D Prescribers - by Provider and Drug (latest, data year 2024)",
     "9552739e-3d05-4c1b-8eff-ecabf391e2e5"),
    # Older (now-dead) UUIDs kept as a breadcrumb of where to look if this rotates:
    ("Medicare Part D Prescribers by Provider and Drug — RY24 (2022) [stale]",
     "9512c660-08b7-49a2-b5b9-3e2b41d6b35e"),
]
# IMPORTANT: Open Payments is NOT on data.cms.gov — it lives on its own portal
# (openpaymentsdata.cms.gov) with a different (DKAN datastore) query API.
# Discovery: https://openpaymentsdata.cms.gov/data.json
OPEN_PAYMENTS_DATASETS = [
    # (label, dataset_id) — verified current 2026-05. General Payment Data.
    ("Open Payments — 2023 General Payment Data",
     "fb3a65aa-c901-4a38-a813-b04b00dfa2a9"),
    ("Open Payments — 2022 General Payment Data",
     "df01c2f8-dc1f-4e79-96cb-8208beaf143c"),
]
# Open Payments program year that the dataset above corresponds to.
OPEN_PAYMENTS_YEAR = 2023

API_BASE = "https://data.cms.gov/data-api/v1/dataset"
# Open Payments DKAN datastore query endpoint (separate host, separate API shape).
OP_API_BASE = "https://openpaymentsdata.cms.gov/api/1/datastore/query"
OP_PAGE_SIZE = 500  # this API caps page size at 500

# The Open Payments columns we keep (machine/lowercase names from the datastore;
# ingest_open_payments.py resolves headers case-insensitively).
OP_WANTED_COLUMNS = [
    "record_id", "program_year", "payment_publication_date", "covered_recipient_npi",
    "covered_recipient_first_name", "covered_recipient_last_name", "recipient_city",
    "recipient_state", "covered_recipient_specialty_1",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
    "total_amount_of_payment_usdollars", "nature_of_payment_or_transfer_of_value",
    "name_of_drug_or_biological_or_device_or_medical_supply_1",
    "product_category_or_therapeutic_area_1",
]
PAGE_SIZE = 5000

# Generic names the demo + current-scope benchmark depend on. They are rare enough
# that a capped broad CA pull can miss them, so after the broad slice we deterministically
# top them up with a targeted per-drug pull, then de-duplicate the whole set. This makes
# `make demo-real` reproduce the Herceptin/Kadcyla demo from scratch, not just by luck.
PART_D_HERO_DRUGS = [
    "Trastuzumab",                # Herceptin — Alice's Q1 / benchmark mkt_01
    "Ado-Trastuzumab Emtansine",  # Kadcyla — Bob's Q2 / the demo's second query
    "Pembrolizumab",              # Keytruda
]
HERO_DRUG_CAP = 20000  # per-drug cap for the targeted top-up


def _pd_key(row: dict) -> tuple:
    """De-dup key for a Part D row: prescriber + brand + generic."""
    return (
        str(row.get("Prscrbr_NPI", "")),
        (row.get("Brnd_Name") or "").upper(),
        (row.get("Gnrc_Name") or "").upper(),
    )


def dedup_part_d_rows(rows: Iterator[dict]) -> Iterator[dict]:
    """Yield rows with duplicate (NPI, brand, generic) keys removed, preserving order.
    Pure + side-effect-free so it can be unit-tested without any network."""
    seen: set = set()
    for row in rows:
        k = _pd_key(row)
        if k in seen:
            continue
        seen.add(k)
        yield row


def fetch_paginated(uuid: str, filters: dict[str, str], max_rows: int) -> Iterator[dict]:
    """Yield rows from a CMS Data API dataset, paginated, with filters applied."""
    offset = 0
    yielded = 0
    while yielded < max_rows:
        # CMS uses bracketed filter syntax: filter[col]=val
        flat_filters = {f"filter[{k}]": v for k, v in filters.items()}
        params = {**flat_filters, "size": min(PAGE_SIZE, max_rows - yielded), "offset": offset}
        url = f"{API_BASE}/{uuid}/data?{urlencode(params)}"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "pharma-rep-copilot/1.0"})
        try:
            with urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 404:
                raise
            print(f"  HTTPError {e.code} at offset {offset}; retrying once after 5s ...")
            time.sleep(5)
            with urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
        if not body:
            return
        for row in body:
            yield row
            yielded += 1
            if yielded >= max_rows:
                return
        if len(body) < params["size"]:
            return  # last page
        offset += params["size"]
        print(f"    ... {yielded:,} rows fetched")


def pick_dataset(candidates: list[tuple[str, str]], filters: dict, probe_only: bool = True) -> tuple[str, str] | None:
    """Try each UUID in order; return the first that responds 200 with at least 1 row."""
    for label, uuid in candidates:
        print(f"  ▶ trying: {label}")
        flat = {f"filter[{k}]": v for k, v in filters.items()}
        params = {**flat, "size": 1}
        url = f"{API_BASE}/{uuid}/data?{urlencode(params)}"
        try:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8"))
            if body:
                print(f"    ✓ live, sample row keys: {list(body[0].keys())[:5]}...")
                return (label, uuid)
            else:
                print("    ⚠ 200 but empty — filter mismatch or dataset rotated")
        except HTTPError as e:
            print(f"    ✗ HTTP {e.code}")
        except Exception as e:
            print(f"    ✗ {e}")
    return None


def download_part_d(state: str, max_rows: int, out_csv: Path):
    print("\n▶ Part D: probing datasets ...")
    filters = {"Prscrbr_State_Abrvtn": state.upper()}
    chosen = pick_dataset(PART_D_DATASETS, filters)
    if not chosen:
        print("✗ All Part D dataset UUIDs failed.")
        print("  Grab the current UUID from:")
        print("  https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers/medicare-part-d-prescribers-by-provider-and-drug")
        print("  Then edit PART_D_DATASETS in this script.")
        return 0

    label, uuid = chosen
    print(f"  Using: {label}")
    print(f"  Streaming up to {max_rows:,} rows to {out_csv} (+ hero-drug top-up) ...")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp file; only atomically rename into place on FULL success, so a
    # mid-stream failure can never leave a partial CSV that a later ingest mistakes
    # for complete data.
    tmp = out_csv.with_suffix(out_csv.suffix + ".tmp")

    def _source():
        # 1) broad CA slice (capped)
        yield from fetch_paginated(uuid, filters, max_rows)
        # 2) deterministic top-up of rare-but-needed drugs (de-duped against the broad pull)
        for drug in PART_D_HERO_DRUGS:
            yield from fetch_paginated(uuid, {**filters, "Gnrc_Name": drug}, HERO_DRUG_CAP)

    n = 0
    present: set = set()
    try:
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = None
            for row in dedup_part_d_rows(_source()):
                if writer is None:
                    writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                    writer.writeheader()
                writer.writerow(row)
                n += 1
                present.add((row.get("Gnrc_Name") or "").upper())
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  ✗ Part D download failed mid-stream: {e}")
        raise
    if n == 0:
        tmp.unlink(missing_ok=True)
        return 0
    tmp.replace(out_csv)
    missing = [d for d in PART_D_HERO_DRUGS if d.upper() not in present]
    if missing:
        print(f"  ⚠ hero drugs absent from the CA pull: {missing} — "
              "the demo/benchmark queries for those may return no rows.")
    print(f"  ✓ wrote {n:,} unique rows")
    return n


def download_open_payments(state: str, max_rows: int, out_csv: Path):
    """Open Payments lives on its OWN portal (openpaymentsdata.cms.gov) with a
    DKAN datastore query API — different host and query shape from Part D.
    We page through CA General Payments and write the columns ingest expects.

    A mid-stream request failure is FATAL (raises) and a partial file is discarded —
    we never silently keep half a download.
    """
    print("\n▶ Open Payments (openpaymentsdata.cms.gov datastore) ...")
    label, ds_id = OPEN_PAYMENTS_DATASETS[0]
    print(f"  Using: {label}")
    print(f"  Streaming up to {max_rows:,} rows to {out_csv} ...")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_csv.with_suffix(out_csv.suffix + ".tmp")
    n = 0
    offset = 0
    try:
        with tmp.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=OP_WANTED_COLUMNS)
            writer.writeheader()
            while n < max_rows:
                params = {
                    "conditions[0][property]": "recipient_state",
                    "conditions[0][value]": state.upper(),
                    "conditions[0][operator]": "=",
                    "limit": min(OP_PAGE_SIZE, max_rows - n),
                    "offset": offset,
                }
                url = f"{OP_API_BASE}/{ds_id}/0?{urlencode(params)}"
                req = Request(url, headers={"Accept": "application/json",
                                            "User-Agent": "pharma-rep-copilot/1.0"})
                with urlopen(req, timeout=60) as r:
                    body = json.loads(r.read().decode("utf-8"))
                results = body.get("results") or []
                if not results:
                    break
                for row in results:
                    writer.writerow({k: row.get(k, "") for k in OP_WANTED_COLUMNS})
                    n += 1
                    if n >= max_rows:
                        break
                print(f"    ... {n:,} rows fetched")
                if len(results) < params["limit"]:
                    break
                offset += len(results)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"  ✗ Open Payments download failed mid-stream: {e}")
        raise
    if n == 0:
        tmp.unlink(missing_ok=True)
        return 0
    tmp.replace(out_csv)
    print(f"  ✓ wrote {n:,} rows")
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="CA")
    p.add_argument("--max-rows", type=int, default=200000,
                   help="Per-dataset cap. Default 200K keeps each download under ~5 minutes.")
    p.add_argument("--part-d-year", type=int, default=2024,
                   help="Program year to STAMP on the Part D rows (the by-provider-and-drug "
                        "data-api serves the latest year; verified 2024 as of 2026-05).")
    p.add_argument("--skip-ingest", action="store_true",
                   help="Just download CSVs to data/raw/; don't COPY into Postgres.")
    p.add_argument("--allow-part-d-only", action="store_true",
                   help="Permit a Part D-only load if Open Payments returns zero rows. "
                        "Without this flag, an empty/failed Open Payments download aborts "
                        "the whole run so the warehouse is never left half-loaded.")
    args = p.parse_args()

    raw = ROOT / "data" / "raw"
    pd_csv = raw / "part_d_via_api.csv"
    op_csv = raw / "open_payments_via_api.csv"

    # A mid-stream failure raises (and discards the temp file); abort the whole run
    # so we never ingest a partial or stale CSV.
    try:
        n_pd = download_part_d(args.state, args.max_rows, pd_csv)
        n_op = download_open_payments(args.state, args.max_rows, op_csv)
    except Exception as e:
        print(f"\n✗ Download aborted: {e}. Nothing was ingested.")
        sys.exit(2)

    if n_pd == 0:
        print("\n✗ Part D download returned 0 rows. Aborting — not ingesting stale data.")
        print("  Check the dataset UUID at https://data.cms.gov/provider-summary-by-type-of-service"
              "/medicare-part-d-prescribers and update PART_D_DATASETS.")
        sys.exit(2)
    if n_op == 0:
        if not args.allow_part_d_only:
            print("\n✗ Open Payments returned 0 rows. Aborting so the warehouse is not left "
                  "half-loaded (Part D only).")
            print("  Re-run with --allow-part-d-only to load Part D alone on purpose, or check "
                  "the dataset id at https://openpaymentsdata.cms.gov/data.json and update "
                  "OPEN_PAYMENTS_DATASETS.")
            sys.exit(3)
        print("\n⚠ Open Payments returned 0 rows — continuing with Part D only (--allow-part-d-only).")

    if args.skip_ingest:
        print(f"\n✓ Downloads complete. {n_pd:,} Part D rows + {n_op:,} Open Payments rows in data/raw/")
        return

    # Re-use the existing ingestion scripts pointing them at the downloaded files.
    # check=True so an ingest failure aborts loudly instead of printing a fake "Done".
    import subprocess
    print(f"\n▶ Ingesting Part D (program year {args.part_d_year}) ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ingest_part_d.py"),
         "--input", str(pd_csv), "--state", args.state, "--year", str(args.part_d_year)],
        check=True,
    )
    if n_op > 0:
        print("\n▶ Ingesting Open Payments ...")
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ingest_open_payments.py"),
             "--input", str(op_csv), "--state", args.state],
            check=True,
        )
    else:
        # Part D-only mode: clear any payments rows left over from a previous run so
        # we never end up with "new Part D + stale Open Payments".
        print("\n▶ Part D-only mode: clearing the payments table so no stale OP rows remain ...")
        _truncate_payments()

    _post_load_check(args.state)
    print("\n✓ Done.")


def _pg_conn():
    import os

    import psycopg
    return psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"), port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DB", "hcp_insights"),
        user=os.getenv("PG_ADMIN_USER", "hcp_admin"),
        password=os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only"),
    )


def _truncate_payments():
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE payments.general_payments")
        print("  ✓ payments.general_payments cleared.")
    except Exception as e:
        print(f"  ⚠ could not clear payments table: {e}")


def _post_load_check(state: str):
    """Confirm the hero demo query (CA trastuzumab/Herceptin) actually has rows — so a
    silently-empty load can't masquerade as success."""
    try:
        with _pg_conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM partd.prescriber_drug_yearly "
                "WHERE prscrbr_state = %s AND UPPER(gnrc_name) LIKE '%%TRASTUZUMAB%%'",
                (state.upper(),),
            )
            n_tz = cur.fetchone()[0]
        if n_tz == 0:
            print("⚠ post-load check: NO trastuzumab (Herceptin) rows — the hero demo query "
                  "will be empty. Check the hero-drug top-up.")
        else:
            print(f"✓ post-load check: {n_tz} trastuzumab rows present — the Herceptin demo query returns data.")
    except Exception as e:
        print(f"(post-load check skipped: {e})")


if __name__ == "__main__":
    main()
