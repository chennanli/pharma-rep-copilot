"""Tests for the HCP fact extractor.

The extractor takes (question, sql, columns, rows) and asks the LLM to
return JSON with structured per-NPI facts. These tests verify:
  - the empty-rows short circuit
  - the NPI column auto-detection (multiple possible column names)
  - the JSON parsing across the 3 fallback paths (raw JSON, ```json fence,
    greedy braces)
  - the validation rules (category whitelist, max length, required fields)
"""
from __future__ import annotations

from unittest.mock import patch

from app.hcp_extractor import (
    _find_npi_column,
    _safe_json_extract,
    extract_facts,
)

# ---------- NPI column detection ----------

def test_find_npi_column_exact_match():
    assert _find_npi_column(["npi", "first_name"]) == "npi"
    assert _find_npi_column(["provider_npi", "x"]) == "provider_npi"


def test_find_npi_column_case_insensitive():
    assert _find_npi_column(["NPI", "First_Name"]) == "NPI"


def test_find_npi_column_fuzzy_suffix():
    """Any column ending in 'npi' should be matched as the NPI column."""
    assert _find_npi_column(["x", "weird_thing_npi"]) == "weird_thing_npi"


def test_find_npi_column_missing_returns_none():
    assert _find_npi_column(["first_name", "last_name", "total"]) is None


# ---------- JSON parsing ----------

def test_safe_json_extract_raw_json():
    text = '{"facts": [{"npi": "1", "category": "clinical_focus", "fact_text": "A"}]}'
    parsed = _safe_json_extract(text)
    assert "facts" in parsed
    assert len(parsed["facts"]) == 1


def test_safe_json_extract_with_code_fence():
    text = """Here is your JSON:
```json
{"facts": [{"npi": "1", "category": "rep_note", "fact_text": "B"}]}
```
"""
    parsed = _safe_json_extract(text)
    assert "facts" in parsed
    assert len(parsed["facts"]) == 1


def test_safe_json_extract_with_prose_around():
    text = ('Sure, here you go: {"facts": [{"npi": "9", "category": '
            '"clinical_focus", "fact_text": "C"}]} (hope this helps)')
    parsed = _safe_json_extract(text)
    assert "facts" in parsed
    assert len(parsed["facts"]) == 1


def test_safe_json_extract_garbage_returns_empty_facts():
    """If we can't find any JSON at all, return {"facts": []} (don't crash)."""
    text = "This is not JSON at all, just prose."
    parsed = _safe_json_extract(text)
    assert parsed == {"facts": []}


# ---------- extract_facts end-to-end (with mocked LLM) ----------

def test_extract_facts_empty_rows_short_circuit():
    """If there are no result rows, no LLM call is made and [] is returned."""
    out = extract_facts(
        question="any", sql="SELECT 1",
        columns=["npi", "x"], rows=[]
    )
    assert out == []


def test_extract_facts_no_npi_column_returns_empty():
    """If there's no recognizable NPI column, we can't attribute facts."""
    out = extract_facts(
        question="any", sql="SELECT 1",
        columns=["foo", "bar"], rows=[{"foo": 1, "bar": 2}]
    )
    assert out == []


def test_extract_facts_normal_path():
    """With valid columns + rows, the LLM is called and well-formed facts pass through."""
    mock_response = (
        '{"facts": ['
        '{"npi": "1234567890", "category": "clinical_focus", '
        '"fact_text": "Oncologist in SF"},'
        '{"npi": "1234567890", "category": "prescribing_pattern", '
        '"fact_text": "Top Herceptin prescriber 2023"}'
        ']}'
    )
    with patch("app.hcp_extractor.llm_chat", return_value=mock_response):
        out = extract_facts(
            question="Top oncologists",
            sql="SELECT npi, name FROM partd",
            columns=["npi", "name"],
            rows=[{"npi": "1234567890", "name": "Dr Chen"}],
        )
    assert len(out) == 2
    assert out[0]["npi"] == "1234567890"
    assert out[0]["category"] == "clinical_focus"


def test_extract_facts_filters_invalid_category():
    """Categories outside the allowed 5-value set must be dropped silently."""
    mock_response = (
        '{"facts": ['
        '{"npi": "1", "category": "made_up_category", "fact_text": "X"},'
        '{"npi": "1", "category": "clinical_focus", "fact_text": "Y"}'
        ']}'
    )
    with patch("app.hcp_extractor.llm_chat", return_value=mock_response):
        out = extract_facts(
            "q", "SELECT npi", ["npi"], [{"npi": "1"}]
        )
    # Only the valid category survives
    assert len(out) == 1
    assert out[0]["category"] == "clinical_focus"


def test_extract_facts_truncates_long_text():
    """fact_text longer than 200 chars should be truncated to keep storage sane."""
    long_text = "A" * 500
    mock_response = (
        f'{{"facts": [{{"npi": "1", "category": "clinical_focus", '
        f'"fact_text": "{long_text}"}}]}}'
    )
    with patch("app.hcp_extractor.llm_chat", return_value=mock_response):
        out = extract_facts(
            "q", "SELECT npi", ["npi"], [{"npi": "1"}]
        )
    assert len(out) == 1
    assert len(out[0]["fact_text"]) <= 200
    assert out[0]["fact_text"].endswith("...")


def test_extract_facts_handles_llm_failure_gracefully():
    """If the LLM call raises, extract_facts must return [] — never propagate."""
    with patch("app.hcp_extractor.llm_chat", side_effect=RuntimeError("boom")):
        out = extract_facts(
            "q", "SELECT npi", ["npi"], [{"npi": "1"}]
        )
    assert out == []
