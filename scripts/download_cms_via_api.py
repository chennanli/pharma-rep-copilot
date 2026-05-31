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
    # Try in order — newest first.
    ("Medicare Part D Prescribers by Provider and Drug — RY24 (2022)",
     "9512c660-08b7-49a2-b5b9-3e2b41d6b35e"),
    ("Medicare Part D Prescribers by Provider and Drug — RY23 (2021)",
     "00c84e8e-9bb8-44e3-a8a7-bf80a3a6abd3"),
]
OPEN_PAYMENTS_DATASETS = [
    # General Payments dataset
    ("Open Payments General Payments — PGYR23 (2023)",
     "ee5d0586-f3cc-4f76-bfc3-d70b29b3d4f8"),
    ("Open Payments General Payments — PGYR22 (2022)",
     "d11a3a4f-3b1f-43f9-bbe2-a3e0e6fe1d39"),
]


API_BASE = "https://data.cms.gov/data-api/v1/dataset"
PAGE_SIZE = 5000


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
    print(f"  Streaming up to {max_rows:,} rows to {out_csv} ...")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = None
        for row in fetch_paginated(uuid, filters, max_rows):
            if writer is None:
                writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                writer.writeheader()
            writer.writerow(row)
            n += 1
    print(f"  ✓ wrote {n:,} rows")
    return n


def download_open_payments(state: str, max_rows: int, out_csv: Path):
    print("\n▶ Open Payments: probing datasets ...")
    filters = {"Recipient_State": state.upper()}
    chosen = pick_dataset(OPEN_PAYMENTS_DATASETS, filters)
    if not chosen:
        print("✗ All Open Payments dataset UUIDs failed.")
        print("  Grab the current UUID from:")
        print("  https://www.cms.gov/openpayments/data/datasetdownloads")
        return 0

    label, uuid = chosen
    print(f"  Using: {label}")
    print(f"  Streaming up to {max_rows:,} rows to {out_csv} ...")
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = None
        for row in fetch_paginated(uuid, filters, max_rows):
            if writer is None:
                writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                writer.writeheader()
            writer.writerow(row)
            n += 1
    print(f"  ✓ wrote {n:,} rows")
    return n


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="CA")
    p.add_argument("--max-rows", type=int, default=200000,
                   help="Per-dataset cap. Default 200K keeps each download under ~5 minutes.")
    p.add_argument("--skip-ingest", action="store_true",
                   help="Just download CSVs to data/raw/; don't COPY into Postgres.")
    args = p.parse_args()

    raw = ROOT / "data" / "raw"
    pd_csv = raw / "part_d_via_api.csv"
    op_csv = raw / "open_payments_via_api.csv"

    n_pd = download_part_d(args.state, args.max_rows, pd_csv)
    n_op = download_open_payments(args.state, args.max_rows, op_csv)

    if args.skip_ingest:
        print(f"\n✓ Downloads complete. {n_pd:,} Part D rows + {n_op:,} Open Payments rows in data/raw/")
        return

    # Re-use the existing ingestion scripts pointing them at the downloaded files
    import subprocess
    print("\n▶ Ingesting Part D ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ingest_part_d.py"),
         "--input", str(pd_csv), "--state", args.state],
        check=False,
    )
    print("\n▶ Ingesting Open Payments ...")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ingest_open_payments.py"),
         "--input", str(op_csv), "--state", args.state],
        check=False,
    )
    print("\n✓ Done.")


if __name__ == "__main__":
    main()
