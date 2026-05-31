"""
SQL Safety Gate.

Standard sqlglot-based AST safety gate, adapted for Postgres dialect. Same
defense-in-depth pattern used by most production text-to-SQL deployments
in regulated environments.

Three jobs:
  1. Parse the candidate SQL with sqlglot. Reject anything that isn't a top-level SELECT.
  2. Auto-inject LIMIT 200 if missing.
  3. Flag single-HCP exposure patterns (per the pharma compliance discussion in docs/safety.md).

This is the LAST line of defense before pg_runner. The Postgres role is also read-only, so even
if this module has a bug, the database will still refuse writes. Defense in depth.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp

FORBIDDEN_ROOT_KINDS = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Grant,
)

# Columns whose appearance in the SELECT list (without GROUP BY) suggests single-HCP exposure.
HCP_IDENTIFYING_COLUMNS = {
    "provider_first_name",
    "provider_last_name_legal_name",
    "provider_middle_name",
    "npi",  # technically a number but it's the unique HCP identifier
}


@dataclass
class SafetyResult:
    ok: bool
    sql: str  # possibly rewritten (LIMIT injected)
    reason: Optional[str] = None
    warnings: tuple[str, ...] = ()


def validate(sql: str, *, dialect: str = "postgres", row_limit: int = 200,
             allow_hcp_detail: bool = False) -> SafetyResult:
    """Validate and normalize a candidate SQL.

    Returns a SafetyResult. On ok=False, do not execute. The error message is human-readable
    and can be fed back to the LLM for self-correction.
    """
    if not sql or not sql.strip():
        return SafetyResult(False, sql, "Empty SQL.")

    # Reject obvious multi-statement payloads
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        return SafetyResult(False, sql, "Multiple statements not allowed.")

    # Reject bind placeholders before parsing — sqlglot would accept them
    for marker in (":", "?", "%s"):
        if marker in stripped:
            # `:` is also legal in some casts like `::int`, so be precise
            if marker == ":" and "::" in stripped:
                # allow Postgres casts; check for bare colon-name pattern via simple heuristic
                import re
                if not re.search(r":[a-zA-Z_]", stripped):
                    continue
            if marker == ":":
                import re
                if not re.search(r":[a-zA-Z_]", stripped):
                    continue
            return SafetyResult(False, sql, f"Bind variable placeholder '{marker}' not allowed.")

    try:
        parsed = sqlglot.parse_one(stripped, dialect=dialect)
    except Exception as e:
        return SafetyResult(False, sql, f"SQL did not parse: {e}")

    if parsed is None:
        return SafetyResult(False, sql, "SQL parsed to empty tree.")

    # Reject forbidden root kinds
    if isinstance(parsed, FORBIDDEN_ROOT_KINDS):
        return SafetyResult(False, sql, f"Statement type {type(parsed).__name__} not allowed.")

    # Accept WITH ... SELECT
    if isinstance(parsed, exp.With):
        body = parsed.this
        if not isinstance(body, exp.Select):
            return SafetyResult(False, sql, "Only SELECT (and WITH ... SELECT) is allowed.")
    elif not isinstance(parsed, exp.Select):
        return SafetyResult(False, sql, f"Top-level statement is not SELECT (got {type(parsed).__name__}).")

    warnings = []

    # Single-HCP exposure check
    select_targets = [c.alias_or_name.lower() for c in parsed.find_all(exp.Column)]
    has_grouping = parsed.find(exp.Group) is not None
    references_identifying = any(c in HCP_IDENTIFYING_COLUMNS for c in select_targets)
    if references_identifying and not has_grouping and not allow_hcp_detail:
        warnings.append(
            "Query references HCP-identifying columns without GROUP BY. "
            "Pass allow_hcp_detail=True if the user explicitly opted in."
        )
        if not allow_hcp_detail:
            return SafetyResult(False, sql,
                                "Single-HCP exposure: SELECTs HCP names without aggregation. "
                                "Add GROUP BY or set allow_hcp_detail=True.",
                                tuple(warnings))

    # Inject LIMIT if missing
    if not parsed.args.get("limit"):
        parsed.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
        warnings.append(f"Injected LIMIT {row_limit}.")

    final_sql = parsed.sql(dialect=dialect)
    return SafetyResult(True, final_sql, None, tuple(warnings))


# ---------- self-test ----------

if __name__ == "__main__":
    cases = [
        # (sql, expected_ok)
        ("SELECT 1", True),
        ("SELECT COUNT(*) FROM npi.npi_registry", True),
        ("DROP TABLE npi.npi_registry", False),
        ("INSERT INTO foo VALUES (1)", False),
        ("SELECT * FROM npi.npi_registry; DROP TABLE foo", False),
        ("SELECT * FROM npi.npi_registry WHERE npi = :npi", False),
        ("UPDATE npi.npi_registry SET npi=1", False),
        ("WITH x AS (SELECT 1) SELECT * FROM x", True),
        ("SELECT provider_first_name, provider_last_name_legal_name FROM npi.npi_registry", False),
        ("SELECT provider_first_name, COUNT(*) FROM npi.npi_registry GROUP BY provider_first_name", True),
    ]
    failed = 0
    for sql, expected_ok in cases:
        r = validate(sql)
        status = "OK" if r.ok == expected_ok else "FAIL"
        if r.ok != expected_ok:
            failed += 1
        print(f"[{status}] expected_ok={expected_ok} got_ok={r.ok} reason={r.reason!r}")
        if r.warnings:
            print(f"        warnings: {r.warnings}")
    print(f"\n{len(cases)-failed}/{len(cases)} passed.")
