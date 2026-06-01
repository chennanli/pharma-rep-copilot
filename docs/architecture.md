# Pharma Rep Copilot — Architecture

> **Range**: End-to-end architecture for the NL2SQL co-pilot over open US pharma commercial datasets.
> **Last updated**: 2026-05-30
> **Relation**: This file is the long-form architecture doc with implementation-level details. For the user-facing walk-through (memory principle, demo arc, quickstart), see the main [`README.md`](../README.md).

---

> ⚠️ **Scope & status — read first.** This document describes the **target architecture / design vision**, not all of which is built. The current repo is a weekend **prototype** that implements a subset.
>
> **Implemented today:** 8-node LangGraph agent (`agent_graph.py`); sqlglot AST safety gate (`sql_safety.py`); read-only Postgres runner (`pg_runner.py`); live schema introspection (`schema_introspect.py`); two-layer team memory (`memory_store.py` + `hcp_extractor.py`); FastAPI + HTMX UI (`main.py`). Data in scope: `payments`, `partd`, `npi`, `reference` (synthetic seed by default, real CMS optional).
>
> **Planned / not yet built:** the `trials` schema + ClinicalTrials.gov ingest; `hcp_resolver.py`, `drug_resolver.py` / RxNorm, `business_hints.py`, `retrievers.py`, query cache; Track B (multi-candidate + selector); the retry / self-correction loop; `EXPLAIN` cost cap; a dedicated `logs/audit.jsonl` (today every query is persisted to the `sql_examples` table instead); the off-label inference guard; `schema_introspect`-after-generation column verification.
>
> Anything below that isn't in the "implemented today" list is a roadmap item, even where the prose is written in the present tense.

---

## 1. Problem Statement

Pharma commercial / medical affairs analysts spend significant time writing SQL or filing tickets to data engineering to answer recurring questions about HCPs (healthcare professionals), prescribing volumes, industry payments, and trial activity. The questions follow predictable patterns but the underlying schemas are large, poorly documented, and span multiple datasets.

A natural-language SQL co-pilot can compress this from "request → 3-day ticket" to "question → 30-second answer", with the analyst retaining final judgment on whether the SQL is correct.

We use only **open public datasets** (CMS, NIH, FDA) so that this prototype is reproducible by anyone without pharma access.

---

## 2. Three-Layer Architecture

Shared infrastructure on the bottom; different entry points on top. Standard pattern for production text-to-SQL with multiple runtime modes (interactive UI, batch API, R&D notebook).

```
                ┌───────────────────────────────────────────┐
                │   Postgres 16 warehouse (local Docker)    │
                │   schemas: payments, partd, npi, trials   │
                │   ~15M+ rows                              │
                └─────────────────┬─────────────────────────┘
                                  │
            ┌─────────────────────┴─────────────────────┐
            │      Shared infrastructure (app/)         │
            │ ─────────────────────────────────────────│
            │  schema_introspect / retrievers /        │
            │  hcp_resolver / drug_resolver /          │
            │  sql_safety (AST gate) / pg_runner /     │
            │  business_hints / prompts / cache        │
            └────┬──────────────────────────────┬──────┘
                 │                              │
       ┌─────────▼──────────┐         ┌────────▼─────────┐
       │ Track A — runtime  │         │ Track B — R&D    │
       │ LangGraph 4–12 nodes│        │ multi-candidate  │
       │ FastAPI POST /ask  │         │ + selector       │
       │ Target: p50 ≤ 60s  │         │ Target: exec→100%│
       │ Single LLM, N=1–2  │         │ N=3–5 + voting   │
       └────────────────────┘         └──────────────────┘
                 │                              │
                 ▼                              ▼
   ┌───────────────────────────┐    ai_sql_example flywheel
   │ CLI / FastAPI / (later)   │    PENDING → VERIFIED →
   │ thin web UI              │    Chroma few-shot fuel
   └───────────────────────────┘
```

**Phase scope**: S0–S2 builds Track A only. Track B is S3+.

---

## 3. Data Sources

All open / public. See `docs/data-sources.md` for full URLs, schemas, refresh cadence.

| Source | Granularity | Why we use it | Approx size |
|---|---|---|---:|
| **CMS Open Payments** | Industry-to-HCP payment record | Map HCP ↔ pharma financial relationships | 10M+ rows/year |
| **Medicare Part D Prescriber** | HCP × drug × year prescribing totals | Quantify HCP prescribing patterns | 25M rows |
| **NPI Registry** | HCP identity, specialty, location | Resolve HCP names / NPIs / specialties | ~7M HCPs |
| **ClinicalTrials.gov** | Trial × investigator × site | Identify PIs and trial activity | ~500K trials |
| **DailyMed / RxNorm** | Drug labels, brand ↔ generic mapping | Drug name normalization | — |

All ingested into Postgres schemas of the same name. Ingestion scripts in `scripts/ingest_*.py`.

---

## 4. Component Map

### 4.1 `app/` — runtime

| Module | Responsibility |
|---|---|
| `agent_graph.py` | LangGraph DAG. S0 is 4 nodes: rewrite → retrieve_schema → gen_sql → exec. |
| `prompts.py` | All LLM prompts in one place. Versioned. |
| `sql_safety.py` | sqlglot AST gate. Rejects non-SELECT, enforces row limit, blocks bind placeholders, rejects PII single-HCP queries. |
| `pg_runner.py` | Postgres connection pool. Read-only role. Statement timeout. |
| `schema_introspect.py` | Live `pg_catalog` queries for table/column lookups. Anti-hallucination. |
| `retrievers.py` | (S1+) Hybrid retrieval: dense (Chroma) + BM25. S0 ships naive top-k. |
| `hcp_resolver.py` | Fuzzy match a free-text HCP reference (e.g. "Dr. Chen in SF") to candidate NPIs. |
| `drug_resolver.py` | Brand/generic/RxNorm resolution. "Herceptin" → trastuzumab → all NDC codes. |
| `business_hints.py` | Domain rules (e.g., "top decile prescriber" = top 10% by volume in that specialty/region). |
| `cli.py` | Local CLI entry point. |
| `main.py` | (S2+) FastAPI server. |

### 4.2 `docs/`

| File | Purpose |
|---|---|
| `architecture.md` | This file. |
| `data-sources.md` | Each dataset: URL, schema overview, refresh cadence, license, gotchas. |
| `safety.md` | AST gate rules. Postgres role config. PII red lines. |

### 4.3 `scripts/`

| Script | Job |
|---|---|
| `setup_postgres.sh` | Spin up Postgres in Docker, create roles. |
| `ingest_open_payments.py` | CSV → Postgres for CMS Open Payments. |
| `ingest_part_d.py` | CSV → Postgres for Medicare Part D Prescriber. |
| `ingest_npi.py` | CSV → Postgres for NPI Registry subset. |
| `ingest_trials.py` | ClinicalTrials.gov API → Postgres. |
| `build_value_index.py` | Pre-compute fuzzy lookup index for HCP names, drug names. |
| `run_benchmark.py` | Run the current-scope eval (6 questions on the loaded CA slice). Emit jsonl + score summary. |
| `analyze_failures.py` | Group failed questions by failure mode. |

### 4.4 `benchmarks/`

- `questions.jsonl` — 6 current-scope questions answerable on the loaded CA slice (`questions-roadmap.jsonl` holds 24 more that need other states / multiple years / geo / single-HCP-detail authorization / the trials+drugs schemas). Each has: `id`, `question`, `expected_tables`, `expected_keywords`, `category`, `difficulty`, `notes`, `scope`.
- `run-*.jsonl` — Per-run outputs. One JSON per question per run.

### 4.5 `.claude/`

- `agents/` — Specialized sub-agent prompts (S3+). For S0–S2, the main loop is single-agent.
- `mcp.json` — (later) MCP server config.

---

## 5. Data Flow (S0 Minimal)

```
User question (English or Chinese)
      │
      ▼
[rewrite]  — normalize the question, detect intent (lookup vs aggregation vs ranking)
      │
      ▼
[retrieve_schema] — naive: dump short summaries of all 4 schemas; later: top-K by embedding+BM25
      │
      ▼
[gen_sql] — LLM call. Returns one candidate SQL.
      │
      ▼
[sql_safety.validate] — AST check. Reject if violates. (returns error to LLM if S0.5+)
      │
      ▼
[exec] — pg_runner runs with LIMIT 200, statement_timeout 30s.
      │
      ▼
Return: { sql, rows, rowcount, elapsed_ms, sources_cited }
```

Phase S1 will add `retrievers.py` with embedding-based schema selection. Phase S2 will add error_correct + retry loop (≤ 3 attempts). Phase S3 will branch into Track B (multi-candidate + selector).

---

## 6. Evaluation

Four orthogonal metrics. The REK composite (recall ∩ exec ∩ kw) is the standard reporting form for NL-to-SQL evaluation — reporting only `exec` is misleading because a query can run and return zero rows.

| Metric | Definition | S0 target | S2 target |
|---|---|---:|---:|
| **recall** | Did the SQL reference all `expected_tables`? | ≥ 60% | ≥ 85% |
| **exec** | Did the SQL run on Postgres without error? | ≥ 60% | ≥ 95% |
| **kw** | Did the result contain at least one `expected_keyword`? | ≥ 40% | ≥ 70% |
| **REK** | All three pass for the same question. | ≥ 30% | ≥ 60% |
| p50 latency | Median end-to-end seconds. | ≤ 30s | ≤ 60s |

The composite REK is the metric the project optimizes against. Reporting only `exec` is misleading — a query can run and return zero rows.

---

## 7. Safety

Detailed in `docs/safety.md`. Three layers:

1. **Database role.** Connection user has only `SELECT` privilege on the four schemas. No `INSERT`, no `UPDATE`, no `CREATE`, no `GRANT`.
2. **AST gate (`app/sql_safety.py`).** sqlglot parses the LLM output. Allowed root statement: `SELECT` (including `WITH ... SELECT`). Anything else: hard reject. Also enforces:
   - No bind variable placeholders (`:foo`, `?`).
   - No multiple statements.
   - Auto-injects `LIMIT 200` if missing.
   - Rejects `SELECT *` if the FROM-set contains tables with HCP-identifying columns unless the query has `GROUP BY` (anti-doxxing for single-HCP exposure).
3. **Traceability.** Today every query is persisted to the `sql_examples` table (question, SQL, author, timestamp) the moment it runs, and every memory write is timestamped and source-attributed. *(Planned: a dedicated `logs/audit.jsonl` with a result hash.)*

---

## 8. Compliance Considerations (Pharma-Specific)

Even though the data is public, we design the architecture as if it were internal. This makes the project a credible demo for regulated environments.

- **Single-HCP exposure check.** If a question would identify the prescribing or payment history of *one individual* HCP without aggregation, the safety gate flags it. Analyst must explicitly opt-in.
- **Citation requirement.** Every result includes which dataset(s) and which version-date were used. Required for Medical Legal Review (MLR) compatibility.
- **Off-label inference guard.** If the question pairs a drug name with an indication that isn't in its FDA label, the agent surfaces a disclaimer and offers to refine.

---

## 9. Open Risks

- **Schema drift.** CMS reformats Open Payments yearly. We pin to specific data versions and detect drift on next ingest.
- **HCP resolution ambiguity.** "Dr. Chen in San Francisco" returns many candidates. We surface top-5 with confidence scores rather than guessing.
- **Drug name ambiguity.** Brand names re-used across markets. We always show which RxNorm/NDC was matched.
- **LLM hallucinated columns.** Mitigation: `schema_introspect` is called *after* generation to verify every referenced column exists. Mismatch → retry with explicit column hint.

---

## 10. Onboarding Path

1. Read this file (you are here).
2. Read `docs/data-sources.md` to understand what's in the warehouse.
3. Read `docs/safety.md` to understand what the agent cannot do.
4. Set up Postgres: `bash scripts/setup_postgres.sh`.
5. Load the California slice: `make demo-real` (or `python scripts/download_cms_via_api.py --state CA`) — synthetic alternative: `make demo`.
6. Run the agent: `python -m app.cli "How many oncologists in California prescribed Herceptin in 2024?"`
7. Run the benchmark: `python scripts/run_benchmark.py` (the 6 current-scope CA questions).
8. Read the main [`README.md`](../README.md) for the user-facing walk-through (the memory principle, the demo arc).
