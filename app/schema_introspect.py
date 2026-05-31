"""
Live schema introspection from Postgres `information_schema`.

Standard live-introspection pattern via `pg_catalog`. The purpose is anti-hallucination:
after the LLM generates a SQL, we can verify every referenced column actually exists
before sending it to pg_runner.

In S0 the simplest use is: dump short summaries of each schema into the prompt.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .pg_runner import _connect

SCHEMAS_IN_SCOPE = ("payments", "partd", "npi", "reference")


@dataclass(frozen=True)
class ColumnInfo:
    schema: str
    table: str
    column: str
    data_type: str
    description: str | None  # from pg_catalog.pg_description if available


@lru_cache(maxsize=1)
def list_tables() -> list[tuple[str, str]]:
    """Return [(schema, table), ...] for the in-scope schemas."""
    sql = """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema = ANY(%s)
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(SCHEMAS_IN_SCOPE),))
            rows = cur.fetchall()
    return [(r[0], r[1]) for r in rows]


@lru_cache(maxsize=1024)
def describe_table(schema: str, table: str) -> list[ColumnInfo]:
    """Return column-level info for one table."""
    sql = """
        SELECT
            c.table_schema, c.table_name, c.column_name, c.data_type,
            pgd.description
        FROM information_schema.columns c
        LEFT JOIN pg_catalog.pg_statio_all_tables st
          ON st.schemaname = c.table_schema AND st.relname = c.table_name
        LEFT JOIN pg_catalog.pg_description pgd
          ON pgd.objoid = st.relid
         AND pgd.objsubid = c.ordinal_position
        WHERE c.table_schema = %s AND c.table_name = %s
        ORDER BY c.ordinal_position
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (schema, table))
            rows = cur.fetchall()
    return [ColumnInfo(*r) for r in rows]


def schema_summary_markdown() -> str:
    """Compact markdown summary suitable for inclusion in an LLM prompt.

    S0 strategy: dump everything. ~3K tokens for the in-scope schemas. Replace with
    embedding-based retrieval in S1.
    """
    lines = []
    for schema, table in list_tables():
        cols = describe_table(schema, table)
        lines.append(f"\n### `{schema}.{table}`")
        for c in cols[:30]:  # cap per-table to keep prompt sane
            desc = f" — {c.description}" if c.description else ""
            lines.append(f"- `{c.column}` ({c.data_type}){desc}")
        if len(cols) > 30:
            lines.append(f"- … and {len(cols) - 30} more columns")
    return "\n".join(lines)


def verify_columns_exist(refs: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Given (schema, table, column) tuples, return the subset that does NOT exist.

    Used post-generation to catch hallucinated columns before exec.
    """
    missing = []
    for s, t, c in refs:
        cols = describe_table(s, t)
        if not any(ci.column.lower() == c.lower() for ci in cols):
            missing.append((s, t, c))
    return missing
