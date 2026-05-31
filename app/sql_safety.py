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

# Data-modifying / DDL node types that must NOT appear ANYWHERE in the tree —
# including nested inside a CTE (e.g. `WITH x AS (DELETE ... RETURNING *) SELECT ...`,
# which Postgres supports). Checked tree-wide, not just at the root.
FORBIDDEN_ANYWHERE_KINDS = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
    exp.Grant, exp.Command,  # exp.Command catches CALL/VACUUM/etc. sqlglot can't fully model
)

# Schemas the agent is allowed to read. Anything else (information_schema, pg_catalog,
# pg_temp, a stray public.*) is rejected so the agent can't probe DB metadata.
ALLOWED_SCHEMAS = {"payments", "partd", "npi", "reference"}

# Functions that have nothing to do with analytics and are classic abuse vectors
# (time-based DoS, file/network access). Rejected by name, case-insensitive.
DENIED_FUNCTIONS = {
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "dblink_exec",
    "query_to_xml", "copy", "pg_terminate_backend", "pg_cancel_backend",
}

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

    # Reject genuine bind-variable placeholders, but do NOT false-reject:
    #   - Postgres `::` casts (e.g. total::numeric)
    #   - `%` or `?` that appear inside string literals (e.g. LIKE '%semaglutide%')
    import re as _re
    # 1) blank out single-quoted string literals so their contents can't trip us
    _scrubbed = _re.sub(r"'(?:[^']|'')*'", "''", stripped)
    # 2) drop Postgres :: cast operators
    _scrubbed = _scrubbed.replace("::", " ")
    if (
        _re.search(r"(?<![:\w]):[a-zA-Z_]\w*", _scrubbed)   # :name  (named bind var)
        or _re.search(r"%\(?\w*\)?s", _scrubbed)            # %s / %(name)s  (psycopg)
        or _re.search(r"(?<!\w)\?(?!\w)", _scrubbed)        # bare ?  (qmark bind var)
    ):
        return SafetyResult(False, sql, "Bind variable placeholder not allowed.")

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

    # Reject data-modifying / DDL nodes ANYWHERE in the tree (e.g. hidden inside a CTE).
    for kind in FORBIDDEN_ANYWHERE_KINDS:
        node = parsed.find(kind)
        if node is not None:
            return SafetyResult(False, sql,
                                f"Disallowed statement type {type(node).__name__} found in the query "
                                f"(data-modifying/DDL is never allowed, even inside a CTE).")

    # Function denylist (time-based DoS, file/network access, etc.)
    for fn in parsed.find_all(exp.Anonymous):
        fname = (fn.name or "").lower()
        if fname in DENIED_FUNCTIONS:
            return SafetyResult(False, sql, f"Function '{fname}()' is not allowed.")
    for fn in parsed.find_all(exp.Func):
        fname = (fn.sql_name() or "").lower()
        if fname in DENIED_FUNCTIONS:
            return SafetyResult(False, sql, f"Function '{fname}()' is not allowed.")

    # Schema allowlist: any schema-qualified table must be in an allowed schema.
    # (Unqualified names are CTE aliases / subquery refs and are fine.)
    for tbl in parsed.find_all(exp.Table):
        schema = (tbl.db or "").lower()
        if schema and schema not in ALLOWED_SCHEMAS:
            return SafetyResult(False, sql,
                                f"Table references schema '{schema}', which is not allowed. "
                                f"Allowed schemas: {', '.join(sorted(ALLOWED_SCHEMAS))}.")

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
