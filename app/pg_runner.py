"""
Postgres read-only runner.

Single source of truth for opening a connection, applying timeouts, and executing SELECTs.
No other module should construct psycopg.connect() directly.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import psycopg
from dotenv import load_dotenv

load_dotenv()


@dataclass
class QueryResult:
    rows: list[dict]
    rowcount: int
    columns: list[str]
    elapsed_ms: int


def _connect() -> psycopg.Connection:
    """Open a connection with the read-only role and statement timeout."""
    conn = psycopg.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=int(os.getenv("PG_PORT", "5432")),
        dbname=os.getenv("PG_DB", "hcp_insights"),
        user=os.getenv("PG_USER", "hcp_agent_ro"),
        password=os.getenv("PG_PASSWORD", ""),
        autocommit=True,
    )
    timeout_s = int(os.getenv("PG_STATEMENT_TIMEOUT", "30"))
    with conn.cursor() as cur:
        cur.execute(f"SET statement_timeout = '{timeout_s}s'")
        cur.execute("SET idle_in_transaction_session_timeout = '60s'")
    return conn


def execute_select(sql: str, max_rows: int = 200) -> QueryResult:
    """Execute a validated SELECT. Returns rows as dicts.

    Caller MUST have already passed sql through app.sql_safety.validate.
    """
    start = time.time()
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows_raw = cur.fetchmany(max_rows)
    elapsed_ms = int((time.time() - start) * 1000)
    rows = [dict(zip(columns, r, strict=False)) for r in rows_raw]
    return QueryResult(rows=rows, rowcount=len(rows), columns=columns, elapsed_ms=elapsed_ms)


def explain(sql: str) -> dict[str, Any]:
    """Run EXPLAIN (FORMAT JSON) on a SELECT. Return the plan as a dict.

    Used by sql_safety for cost cap. Not used in S0 minimal path.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("EXPLAIN (FORMAT JSON) " + sql)
            (plan,) = cur.fetchone()  # type: ignore
    return plan[0] if isinstance(plan, list) else plan
