"""Tests for the SQL safety gate.

The gate is the LAST line of defense before any SQL touches Postgres. These
tests promote the hostile-input cases that previously only lived in the
module's `if __name__ == "__main__":` block, plus a few additional edge
cases for the LIMIT auto-injection and the multi-statement detection.
"""
from __future__ import annotations

import pytest

from app.sql_safety import validate

# ---------- happy path: legitimate SELECTs ----------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "SELECT COUNT(*) FROM npi.npi_registry",
    "WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT provider_first_name, COUNT(*) FROM npi.npi_registry "
    "GROUP BY provider_first_name",
])
def test_safe_selects_pass(sql):
    """Every legitimate SELECT must pass the gate with ok=True."""
    r = validate(sql)
    assert r.ok is True, f"expected ok=True but got reason={r.reason!r}"


# ---------- hostile path: must be rejected ----------

@pytest.mark.parametrize("sql,reason_keyword", [
    # DDL
    ("DROP TABLE npi.npi_registry",     "not allowed"),
    ("CREATE TABLE foo (x INT)",        "not allowed"),
    ("ALTER TABLE npi.npi_registry ADD COLUMN x INT", "not allowed"),
    # DML
    ("INSERT INTO foo VALUES (1)",      "not allowed"),
    ("UPDATE npi.npi_registry SET npi=1", "not allowed"),
    ("DELETE FROM npi.npi_registry",    "not allowed"),
    # Multi-statement injection
    ("SELECT * FROM npi.npi_registry; DROP TABLE foo", "Multiple statements"),
    # Bind variable injection
    ("SELECT * FROM npi.npi_registry WHERE npi = :npi", "Bind variable"),
])
def test_hostile_inputs_rejected(sql, reason_keyword):
    """Every hostile input must be rejected with a human-readable reason."""
    r = validate(sql)
    assert r.ok is False, f"hostile SQL was accepted: {sql!r}"
    assert reason_keyword.lower() in r.reason.lower(), (
        f"expected reason to mention {reason_keyword!r} but got {r.reason!r}"
    )


# ---------- single-HCP exposure check ----------

def test_select_hcp_identifying_columns_without_group_by_rejected():
    """Selecting HCP-identifying columns without aggregation is a privacy risk."""
    sql = ("SELECT provider_first_name, provider_last_name_legal_name "
           "FROM npi.npi_registry")
    r = validate(sql)
    assert r.ok is False
    assert "single-hcp" in r.reason.lower() or "exposure" in r.reason.lower()


def test_select_hcp_columns_with_group_by_allowed():
    """Aggregation makes single-HCP exposure impossible; should pass."""
    sql = ("SELECT provider_first_name, COUNT(*) FROM npi.npi_registry "
           "GROUP BY provider_first_name")
    r = validate(sql)
    assert r.ok is True, f"reason: {r.reason!r}"


def test_select_hcp_columns_with_explicit_opt_in_allowed():
    """When the caller explicitly opts in via allow_hcp_detail, it should pass."""
    sql = ("SELECT provider_first_name, provider_last_name_legal_name "
           "FROM npi.npi_registry WHERE npi = '1234567890'")
    r = validate(sql, allow_hcp_detail=True)
    assert r.ok is True, f"reason: {r.reason!r}"


# ---------- LIMIT auto-injection ----------

def test_limit_auto_injected_when_missing():
    """If the LLM forgets a LIMIT, the gate must add one to bound result size."""
    sql = "SELECT 1"
    r = validate(sql, row_limit=200)
    assert r.ok is True
    assert "limit" in r.sql.lower()
    assert "200" in r.sql
    # The warning should mention the auto-injection
    assert any("limit" in w.lower() for w in r.warnings)


def test_existing_limit_preserved():
    """If the SQL already has a LIMIT, the gate must NOT clobber it."""
    sql = "SELECT 1 LIMIT 5"
    r = validate(sql, row_limit=200)
    assert r.ok is True
    assert "limit 5" in r.sql.lower() or "LIMIT 5" in r.sql


# ---------- robustness ----------

def test_empty_input_rejected():
    """An empty string must be rejected, not crash the gate."""
    r = validate("")
    assert r.ok is False
    assert "empty" in r.reason.lower()


def test_whitespace_only_input_rejected():
    """Only whitespace must be rejected, not crash the gate."""
    r = validate("   \n\t  ")
    assert r.ok is False


def test_unparseable_sql_rejected_gracefully():
    """Garbage input should be rejected with a clear reason, not raise."""
    r = validate("this is not SQL at all !@#$%")
    assert r.ok is False
    # Either fails to parse OR fails type check; both are acceptable
    assert r.reason is not None


# ---------- hardening: nested DML, dangerous functions, schema allowlist ----------

@pytest.mark.parametrize("sql", [
    # data-modifying statement hidden inside a CTE (Postgres allows this) must be rejected
    "WITH t AS (DELETE FROM partd.prescriber_drug_yearly RETURNING *) SELECT * FROM t",
    "WITH t AS (UPDATE npi.npi_registry SET npi='x' RETURNING *) SELECT * FROM t",
    # time-based DoS / file-access functions
    "SELECT pg_sleep(10)",
    "SELECT pg_read_file('/etc/passwd')",
    # metadata probing via non-allowed schemas
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM pg_catalog.pg_tables",
])
def test_rejects_hardened_cases(sql):
    assert validate(sql).ok is False


@pytest.mark.parametrize("sql", [
    "SELECT prscrbr_npi, SUM(tot_clms) FROM partd.prescriber_drug_yearly "
    "WHERE prscrbr_state = 'CA' GROUP BY prscrbr_npi",
    "WITH d AS (SELECT generic FROM reference.drug_alias WHERE brand ILIKE 'herceptin') "
    "SELECT COUNT(*) FROM payments.general_payments",
])
def test_allows_legit_allowed_schema_queries(sql):
    assert validate(sql).ok is True
