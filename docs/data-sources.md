# Data Sources

All sources are public and reproducible. Designed so anyone can build this without internal pharma access.

> ⚠️ **Scope & status — read first.** This describes the **target** set of data sources. The **current build loads only**: Medicare Part D 2024 (by provider and drug, California) + Open Payments 2023 (General Payments, California) + an `npi.npi_registry` derived from the Part D NPIs + a small hand-curated `reference.drug_alias` seed. The ClinicalTrials.gov (`trials.*`), DailyMed/RxNorm (`drugs.*`), and the Open Payments `research_payments` / `ownership` tables below are **planned, not built**. The one reproducible command today is `make demo-real` (or `python scripts/download_cms_via_api.py --state CA`).

---

## 1. CMS Open Payments (Sunshine Act)

**What**: Every payment or transfer of value from a drug or device manufacturer to a US physician or teaching hospital.

**URL**: https://openpaymentsdata.cms.gov/

**Download**: Yearly CSV bundles. Each year is ~1.5 GB compressed, 10M+ rows.

**Key tables (after ingest)**:
- `payments.general_payments` — meals, consulting, travel, honoraria
- `payments.research_payments` — clinical research payments
- `payments.ownership` — physician ownership/investment in companies

**Key columns**:
- `covered_recipient_npi` — joins to `npi.npi_registry.npi`
- `covered_recipient_specialty` — physician specialty
- `total_amount_of_payment_usdollars`
- `nature_of_payment_or_transfer_of_value` — meal, travel, consulting, etc.
- `applicable_manufacturer_or_applicable_gpo_making_payment_name` — sponsor company
- `date_of_payment`

**Gotchas**:
- Names have inconsistent casing.
- Specialty codes use NUCC taxonomy — same as NPI but pre-mapped here.
- Some payments are reported under teaching hospitals, not individual physicians.

**Refresh**: Updated June each year for prior calendar year.

**License**: Public domain (US Government work).

---

## 2. Medicare Part D Prescriber

**What**: Aggregate prescribing volume by HCP × drug × year for Medicare Part D beneficiaries.

**URL**: https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers

**Download**: Yearly CSV. ~25M rows per year (HCP × drug combinations).

**Key table**:
- `partd.prescriber_drug_yearly`

**Key columns**:
- `prscrbr_npi` — HCP NPI, joins to NPI Registry
- `brnd_name`, `gnrc_name` — drug name (brand and generic)
- `tot_clms` — total claims (prescriptions)
- `tot_30day_fills` — normalized 30-day equivalents
- `tot_drug_cst` — total cost
- `tot_benes` — total beneficiaries

**Gotchas**:
- "Suppressed" values when fewer than 11 beneficiaries — appears as null. Important for low-volume drugs.
- Only covers Medicare Part D. Doesn't include private insurance, Medicaid, or 340B.
- Lags by ~18 months from end of calendar year.

**Refresh**: Annually, ~April-May for data from 18 months prior.

---

## 3. NPI Registry

**What**: National Provider Identifier — every licensed HCP in the US.

**URL**: https://download.cms.gov/nppes/NPI_Files.html

**Download**: Monthly full export, ~700 MB CSV, ~7M records.

**Key table**:
- `npi.npi_registry`

**Key columns**:
- `npi` — 10-digit identifier
- `provider_first_name`, `provider_last_name_legal_name`
- `provider_business_practice_location_state`, `…city`, `…postal_code`
- `healthcare_provider_taxonomy_code_1` — specialty (NUCC code)
- `entity_type_code` — 1 = individual, 2 = organization

**Gotchas**:
- ~30% of records are organizations, not individual providers.
- Some HCPs hold multiple specialties (taxonomy_2, taxonomy_3 …).
- Address fields can be the practice address, billing address, or mailing — three sets of columns.

**Refresh**: Monthly.

---

## 4. ClinicalTrials.gov

**What**: Public registry of clinical trials and observational studies.

**URL**: https://clinicaltrials.gov/data-api/api

**Download**: REST API. JSON. We pull subsets relevant to our drugs/indications of interest.

**Key tables (after ingest)**:
- `trials.studies` — one row per NCT ID
- `trials.investigators` — site PIs and sub-investigators
- `trials.sites` — geographic facilities
- `trials.interventions` — drug/device used in each trial

**Key columns** (in `studies`):
- `nct_id`
- `brief_title`, `official_title`
- `study_type` — Interventional / Observational
- `phase` — 1, 2, 3, 4
- `overall_status`
- `conditions` — list of conditions/indications
- `start_date`, `completion_date`
- `sponsors_lead_sponsor_name`

**Gotchas**:
- Investigator NPIs are NOT in the public data. We match by name + affiliation (fuzzy).
- Drug names in `interventions` are free-text — needs RxNorm normalization.
- Trial status updates are not real-time. Re-pull periodically.

**Refresh**: Daily, but for our purposes weekly is fine.

---

## 5. DailyMed / RxNorm

**What**: FDA-approved drug labels (DailyMed) and a standardized drug terminology (RxNorm).

**URLs**:
- DailyMed: https://dailymed.nlm.nih.gov/dailymed/services/v2/
- RxNorm: https://rxnav.nlm.nih.gov/api.html

**Use in this project**:
- **Drug name resolution.** "Herceptin" (brand) → trastuzumab (generic) → RxCUI 224905 → matches to NDC codes in Part D and trial interventions in ClinicalTrials.gov.
- **Indication grounding.** Cross-reference whether a question asks about an off-label use.

**Tables**:
- `drugs.rxnorm_concepts` — small subset of relevant RxCUIs (we curate to our scope)
- `drugs.dailymed_labels_lite` — extracted indication + dosing sections

---

## 6. Load Order

```
1. NPI Registry          (independent, foundational)
2. Open Payments          (joins to NPI)
3. Part D                 (joins to NPI)
4. ClinicalTrials.gov     (independent; HCP fuzzy match optional)
5. RxNorm/DailyMed        (drug normalization layer)
```

*(Target order. The current build only does steps 2–3 plus a derived NPI table — see the scope note at the top.)*

---

## 7. Reproducing the current (California) build

For a laptop, the current real-data path loads a California slice in one command:

```bash
make demo-real
# equivalently, just the download+ingest:
python scripts/download_cms_via_api.py --state CA
```

This pulls Medicare Part D 2024 (by provider and drug, CA) — with a deterministic
top-up of the demo's hero drugs (trastuzumab/Herceptin, ado-trastuzumab/Kadcyla,
pembrolizumab/Keytruda) and de-duplication — plus Open Payments 2023 (General
Payments, CA), then derives `npi.npi_registry` from the Part D NPIs. Enough to run
the **6 current-scope benchmark questions** (`benchmarks/questions.jsonl`); the other
24 are roadmap (`benchmarks/questions-roadmap.jsonl`).
