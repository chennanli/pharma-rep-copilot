"""
Fallback: synthesize a small but realistic-looking dataset.

Use this if you don't want to download 8 GB of CMS CSVs just to run the demo.
The data is plausible but fabricated; suitable for a working end-to-end
demo (the UI, the agent, both memory layers all function), NOT for any
real analysis or shipping.

Generates ~5K Part D rows, ~5K Open Payments rows, ~500 distinct NPIs,
all in California, focused on oncology + endocrinology + cardiology
specialties so the "Top 10 oncologists prescribing Herceptin" demo
arc finds real candidates.

Usage:
    python scripts/synth_seed.py
"""
from __future__ import annotations

import os
import random
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

random.seed(42)

CITIES = [
    "San Francisco", "Los Angeles", "San Diego", "Sacramento", "Oakland",
    "San Jose", "Palo Alto", "Berkeley", "Long Beach", "Fresno",
    "Pasadena", "Santa Monica", "Irvine", "Anaheim", "Burbank",
]
SPECIALTIES = [
    ("Hematology/Oncology", 100),
    ("Medical Oncology", 80),
    ("Endocrinology", 60),
    ("Cardiology", 80),
    ("Internal Medicine", 120),
    ("Family Practice", 60),
]
LAST_NAMES = ["Chen", "Patel", "Garcia", "Kim", "Singh", "Wong", "Nguyen", "Park",
              "Lopez", "Martinez", "Brown", "Lee", "Wilson", "Davis", "Anderson",
              "Taylor", "Thomas", "Moore", "Wang", "Liu"]
FIRST_NAMES = ["Sarah", "David", "Michael", "Jennifer", "James", "Linda", "Robert",
               "Emily", "John", "Maria", "William", "Jessica", "Christopher",
               "Susan", "Daniel", "Karen", "Matthew", "Lisa", "Andrew", "Anna"]

# (brand, generic, who-typically-prescribes) — restrict to drugs that the demo arc covers
DRUGS = [
    ("HERCEPTIN",  "TRASTUZUMAB",                "oncology"),
    ("KADCYLA",    "ADO-TRASTUZUMAB EMTANSINE",  "oncology"),
    ("PERJETA",    "PERTUZUMAB",                 "oncology"),
    ("KEYTRUDA",   "PEMBROLIZUMAB",              "oncology"),
    ("OPDIVO",     "NIVOLUMAB",                  "oncology"),
    ("OZEMPIC",    "SEMAGLUTIDE",                "endocrinology"),
    ("MOUNJARO",   "TIRZEPATIDE",                "endocrinology"),
    ("ENTRESTO",   "SACUBITRIL/VALSARTAN",       "cardiology"),
    ("LIPITOR",    "ATORVASTATIN",               "cardiology"),
    ("CRESTOR",    "ROSUVASTATIN",               "cardiology"),
]
MANUFACTURERS = ["Roche Diagnostics Corporation", "Merck Sharp & Dohme",
                 "Bristol-Myers Squibb Company", "Novo Nordisk Inc.", "Eli Lilly and Company",
                 "Pfizer Inc.", "AstraZeneca Pharmaceuticals LP", "Novartis Pharmaceuticals Corporation",
                 "Sanofi US Services Inc.", "GlaxoSmithKline LLC", "Amgen Inc.", "AbbVie Inc."]
NATURE_OF_PAYMENT = ["Consulting Fee", "Travel and Lodging", "Food and Beverage",
                     "Education", "Honoraria", "Grant"]


def specialty_to_drug_pool(spec: str) -> list[tuple]:
    s = spec.lower()
    if "oncolog" in s:
        return [d for d in DRUGS if d[2] == "oncology"]
    if "endo" in s:
        return [d for d in DRUGS if d[2] == "endocrinology"]
    if "cardio" in s:
        return [d for d in DRUGS if d[2] == "cardiology"]
    return DRUGS


def gen_npi() -> str:
    """Synthesize a 10-digit NPI-looking string. NOT a valid NPI checksum."""
    return "9" + "".join(str(random.randint(0, 9)) for _ in range(9))


def make_hcps(n=500):
    hcps = []
    weights = [w for _, w in SPECIALTIES]
    specs = [s for s, _ in SPECIALTIES]
    for _ in range(n):
        spec = random.choices(specs, weights=weights, k=1)[0]
        hcps.append({
            "npi": gen_npi(),
            "first": random.choice(FIRST_NAMES),
            "last": random.choice(LAST_NAMES),
            "city": random.choice(CITIES),
            "specialty": spec,
        })
    return hcps


def main():
    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    db = os.getenv("PG_DB", "hcp_insights")
    user = os.getenv("PG_ADMIN_USER", "hcp_admin")
    pw = os.getenv("PG_ADMIN_PASSWORD", "admin-dev-only")

    print("▶ Generating synthetic HCPs ...")
    hcps = make_hcps(500)
    print(f"  ✓ {len(hcps)} HCPs")

    print("▶ Generating Part D rows ...")
    part_d_rows = []
    for hcp in hcps:
        pool = specialty_to_drug_pool(hcp["specialty"])
        # Each HCP prescribes 3-8 distinct drugs
        for drug in random.sample(pool, k=min(len(pool), random.randint(2, min(5, len(pool))))):
            tot_clms = random.randint(11, 600)  # CMS suppresses <11
            part_d_rows.append((
                hcp["npi"], hcp["last"], hcp["first"], hcp["city"], "CA",
                hcp["specialty"],
                drug[0],  # brand (sometimes CMS leaves brand NULL — but for demo we keep it)
                drug[1],  # generic
                2023,
                tot_clms,
                round(tot_clms * random.uniform(0.8, 1.5), 1),
                tot_clms * random.randint(20, 60),
                round(tot_clms * random.uniform(40.0, 6000.0), 2),
            ))
    print(f"  ✓ {len(part_d_rows)} Part D rows")

    print("▶ Generating Open Payments rows ...")
    op_rows = []
    record_id = 100_000_000
    for hcp in hcps:
        n_payments = random.randint(0, 12)
        for _ in range(n_payments):
            record_id += 1
            pool = specialty_to_drug_pool(hcp["specialty"])
            drug = random.choice(pool)
            op_rows.append((
                record_id, 2023, "2024-06-30",
                hcp["npi"], hcp["first"], hcp["last"], hcp["city"], "CA",
                hcp["specialty"],
                random.choice(MANUFACTURERS),
                round(random.uniform(15.0, 6500.0), 2),
                random.choice(NATURE_OF_PAYMENT),
                drug[0], drug[2],
            ))
    print(f"  ✓ {len(op_rows)} Open Payments rows")

    print(f"▶ Loading into Postgres {host}:{port}/{db} ...")
    with psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE partd.prescriber_drug_yearly")
            cur.execute("TRUNCATE payments.general_payments")
            cur.execute("TRUNCATE npi.npi_registry")

            cur.executemany(
                """INSERT INTO partd.prescriber_drug_yearly
                   (prscrbr_npi, prscrbr_last, prscrbr_first, prscrbr_city, prscrbr_state,
                    prscrbr_type, brnd_name, gnrc_name, year,
                    tot_clms, tot_30day_fills, tot_day_suply, tot_drug_cst)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                part_d_rows,
            )
            cur.executemany(
                """INSERT INTO payments.general_payments
                   (record_id, program_year, payment_publication_date,
                    covered_recipient_npi, recipient_first_name, recipient_last_name,
                    recipient_city, recipient_state, recipient_specialty,
                    applicable_manufacturer, total_amount_of_payment_usdollars,
                    nature_of_payment, product_name, product_indication)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                op_rows,
            )
            cur.executemany(
                """INSERT INTO npi.npi_registry
                   (npi, first_name, last_name, city, state, specialty)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [(h["npi"], h["first"], h["last"], h["city"], "CA", h["specialty"]) for h in hcps],
            )
        conn.commit()
    print("✓ Synthetic seed complete. Run: bash scripts/setup_postgres.sh first if you haven't.")
    print("  Then: python scripts/seed_drug_alias.py")


if __name__ == "__main__":
    main()
