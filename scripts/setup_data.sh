#!/usr/bin/env bash
# End-to-end data setup orchestrator.
#
# Step 1 — checks data/raw/ for the two CSVs we need.
# Step 2 — if missing, prints exact download URLs and the target filenames it expects,
#          then exits with a clear instruction.
# Step 3 — when both are present, runs the ingestion scripts in the right order:
#            ingest_part_d → derive_npi_registry → ingest_open_payments → seed_drug_alias
#
# Idempotent — safe to re-run. Each ingest TRUNCATEs its target table first.
#
# Usage:
#   bash scripts/setup_data.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RAW="$ROOT/data/raw"
VENV_PY="$ROOT/.venv/bin/python"
PY="${VENV_PY:-python3}"
if [ ! -x "$VENV_PY" ]; then
  PY="python3"
fi

mkdir -p "$RAW"

PARTD="$RAW/medicare_part_d_prescriber_by_provider_and_drug.csv"
OPGEN="$RAW/open_payments_general.csv"

need_input=0
if [ ! -f "$PARTD" ]; then need_input=1; fi
if [ ! -f "$OPGEN" ]; then need_input=1; fi

if [ "$need_input" = "1" ]; then
  cat <<EOF

You need to download two CSVs from CMS and place them in:
  $RAW/

================================================================================
(1) Medicare Part D Prescribers by Provider AND Drug — latest year (currently 2024)
    https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers/medicare-part-d-prescribers-by-provider-and-drug
    • Click "Download" → take the full CSV (~1 GB compressed, ~5 GB unzipped).
    • The file is named something like:
        MUP_DPR_RY26_P04_V10_DY24_NPIBN.csv
    • Rename or symlink to:
        medicare_part_d_prescriber_by_provider_and_drug.csv
    • Expected at:
        $PARTD

(2) CMS Open Payments — General Payments — latest program year (currently 2023)
    https://www.cms.gov/openpayments/data/datasetdownloads
    • Click "Detailed Dataset for {YEAR}" → download the ZIP (~3 GB).
    • Unzip; the file you want is:
        OP_DTL_GNRL_PGYR{YEAR}_P<date>.csv
    • Rename or symlink to:
        open_payments_general.csv
    • Expected at:
        $OPGEN
================================================================================

After both files are in place, re-run:
  bash scripts/setup_data.sh

(Quick option: if you only want the demo to be runnable end-to-end without 8 GB of
downloads, you can use a synthetic mini-dataset — see scripts/synth_seed.py for a
~5-minute seed of plausible-looking rows. The demo will be visibly small but every
moving piece works.)
EOF
  exit 2
fi

echo "▶ Inputs found. Running ingestion pipeline."
echo

echo "── Step 1/4: ingest Part D (filter CA, may take 5-10 min) ──"
"$PY" "$ROOT/scripts/ingest_part_d.py" --state CA --year 2022

echo
echo "── Step 2/4: derive npi.npi_registry from Part D distinct NPIs ──"
"$PY" "$ROOT/scripts/derive_npi_registry.py"

echo
echo "── Step 3/4: ingest Open Payments General (filter CA, may take 5-10 min) ──"
"$PY" "$ROOT/scripts/ingest_open_payments.py" --state CA

echo
echo "── Step 4/4: seed reference.drug_alias ──"
"$PY" "$ROOT/scripts/seed_drug_alias.py"

echo
echo "✓ Data setup complete."
echo
echo "Try a query:"
echo "  $PY -m app.cli \"Top 10 oncologists in California prescribing Herceptin in 2023\""
echo
echo "Or start the web UI:"
echo "  $PY -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload"
