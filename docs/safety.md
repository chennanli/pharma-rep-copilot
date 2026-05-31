# Safety

Three layers of defense — standard defense-in-depth pattern for production text-to-SQL in regulated environments.

> ⚠️ **Scope & status — read first.** This describes the **target** safety design; the prototype implements a subset.
>
> **Implemented today:** Layer 1 (read-only `hcp_agent_ro` Postgres role over `payments` / `partd` / `npi` / `reference`, with a statement timeout) and the core of Layer 2 (sqlglot AST gate: SELECT-only, no multi-statement, no bind placeholders, auto-`LIMIT 200`, single-HCP-exposure check).
>
> **Planned / not yet built:** the `EXPLAIN` cost cap (rule 6); the reject → LLM self-correct/retry loop; the `logs/audit.jsonl` audit log (Layer 3 — today every query is persisted to the `sql_examples` table instead); the off-label inference guard; and version-dated MLR citations (today `sources` are dataset-level, without version). The `trials` / `drugs` schemas referenced in the example GRANT below are also not in the current build.

---

## Layer 1 — Postgres Role

The agent connects as role `hcp_agent_ro`. It has only `USAGE` on the four schemas and `SELECT` on tables. No `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `GRANT`, `TRUNCATE`.

```sql
-- One-time setup, run by a superuser (NOT by the agent)
CREATE ROLE hcp_agent_ro LOGIN PASSWORD '<from .env>';

GRANT USAGE ON SCHEMA payments, partd, npi, trials, drugs TO hcp_agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA payments, partd, npi, trials, drugs TO hcp_agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA payments, partd, npi, trials, drugs
  GRANT SELECT ON TABLES TO hcp_agent_ro;

-- statement timeout per session
ALTER ROLE hcp_agent_ro SET statement_timeout = '30s';
ALTER ROLE hcp_agent_ro SET idle_in_transaction_session_timeout = '60s';
```

Even if the AST gate had a bug, Postgres would reject any non-SELECT from this role.

---

## Layer 2 — AST Safety Gate

Lives in `app/sql_safety.py`. Uses `sqlglot` to parse the LLM output and check structure.

**Rules**:

1. The top-level statement must be `SELECT` (or `WITH ... SELECT`). If parsing produces any of `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `GRANT`, `REVOKE`, `CALL`, `EXECUTE` — hard reject.
2. No semicolons except a trailing one (rejects multiple statements).
3. No bind variable placeholders (`:foo`, `?`, `%s`).
4. Auto-injects `LIMIT 200` if no `LIMIT` clause present.
5. If the FROM-set references `npi.npi_registry` or any payment/prescribing table without a `GROUP BY` and the SELECT-list includes `provider_first_name` or `provider_last_name_legal_name` directly — flag as single-HCP-exposure and require explicit `--allow-hcp-detail` flag in caller context.
6. *(planned)* Estimated cost: run `EXPLAIN` first. If `total_cost > 5,000,000` — reject and ask LLM to add filters.

**Failure mode**: when the gate rejects, the human-readable reason is returned to the caller. *(Planned: feed it back to the LLM for self-correction on retry — there is no retry loop yet.)*

---

## Layer 3 — Audit Log *(planned)*

> Not yet built. Today every query is persisted to the `sql_examples` table (question, SQL, author, timestamp) the moment it runs. The dedicated append-only log below is the target design:

The planned design: every query (whether passed or rejected) appends a JSON line to `logs/audit.jsonl`:

```json
{
  "ts": "2026-05-25T14:31:02Z",
  "trace_id": "run-20260525-q12",
  "question": "Top oncologists in CA by Herceptin Rx in 2023",
  "sql": "SELECT ...",
  "safety_decision": "passed" | "rejected",
  "safety_reason": "...",
  "rowcount": 47,
  "elapsed_ms": 18432,
  "result_hash": "sha256:..."
}
```

`result_hash` is the SHA-256 of the sorted serialized rows. Lets us detect identical query results without storing the rows themselves.

---

## Pharma-Specific Red Lines (Even on Public Data)

Even though Open Payments / Part D / NPI are public, we model this as if it were internal pharma data — because that's the actual deployment scenario this prototype is demonstrating.

### Off-Label Indication Inference *(planned)*

The intended design (not yet implemented): if a question pairs a drug with an indication that isn't in the FDA label (from DailyMed), the LLM is instructed (via system prompt) to:
1. Surface a disclaimer about off-label.
2. Run the query anyway but tag the output with `off_label_flag: true`.
3. NEVER generate copy or recommendations that promote the off-label use.

### Single-HCP Exposure

The Sunshine Act data is *intended* to be public per-physician. But pharma analysts in commercial functions are generally restricted from doxxing individual HCPs in deliverables. Default: surface only aggregated views unless the question explicitly asks for individual detail.

### MLR-Compatible Citation

Every result returns a `sources` field:

```json
{
  "sources": [
    {"dataset": "CMS Open Payments", "version": "2023-program-year", "url": "..."},
    {"dataset": "Medicare Part D Prescriber", "version": "2022-publication", "url": "..."}
  ]
}
```

This makes the output drop-in compatible with downstream Medical Legal Review tools that require source attribution.

---

## What This Protects Against

| Threat | Mitigation |
|---|---|
| LLM hallucinates `DROP TABLE` | AST gate + Postgres role |
| LLM writes `INSERT … SELECT …` to exfiltrate | AST gate + Postgres role |
| Runaway query DoS | `statement_timeout` + EXPLAIN cost reject |
| Result tampering by intermediate code | Audit log result hash |
| Single-HCP doxxing | Single-HCP exposure check |
| Off-label promotion | Off-label flag + system prompt instruction |
| Untraceable analyst claim | Source citation required |
