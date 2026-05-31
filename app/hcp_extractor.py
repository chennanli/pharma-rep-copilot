"""
Background extractor that turns a SQL result set into structured HCP facts.

Given a question, SQL, and the rows that came back, this module:
  1. Detects whether the result rows are HCP-shaped (have an NPI column).
  2. For each HCP row, generates 1-3 atomic facts in our 5-category schema.
  3. Writes them into MemoryStore as fact_type='AUTO'.

Runs in the background (asyncio.create_task) so the user gets results immediately.

Cost note: 1 LLM call per query, regardless of row count. The prompt summarizes all rows
in one go to control token spend.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from dotenv import load_dotenv

from .llm_client import chat as llm_chat
from .memory_store import MemoryStore

load_dotenv()


# Tolerated NPI column aliases the LLM might have used
_NPI_COL_CANDIDATES = ("npi", "provider_npi", "prescriber_npi", "physician_profile_id",
                       "covered_recipient_npi", "n.npi", "p.npi")


VALID_CATEGORIES = {
    "clinical_focus", "prescribing_pattern", "payment_history",
    "rep_note", "affiliation",
}


_SYSTEM = """\
You read a SQL query result about US healthcare providers and emit short structured "facts"
about each provider, one or two per row. Each fact has:
  - npi (string, 10 digits)
  - category (one of: clinical_focus, prescribing_pattern, payment_history, affiliation)
  - fact_text (one short sentence, <=140 chars, factual, present tense, no speculation)

NEVER emit rep_note — that category is reserved for human reps to fill in manually.

Output strict JSON: {"facts": [{"npi":"...","category":"...","fact_text":"..."}, ...]}
No markdown, no prose, no explanation. If there are no HCP rows, return {"facts": []}.

Be conservative. Only state facts directly supported by the row data. Do not invent
specialties or affiliations that aren't in the row. Specifically:
  - prescribing_pattern facts come from Part D columns (drug, claims, totals)
  - payment_history facts come from Open Payments columns (amount, manufacturer)
  - clinical_focus facts come from NPI taxonomy / specialty if present
  - affiliation facts come from city + state + practice name if present
"""


def _find_npi_column(columns: list[str]) -> Optional[str]:
    lower = {c.lower(): c for c in columns}
    for cand in _NPI_COL_CANDIDATES:
        if cand in lower:
            return lower[cand]
    # fuzzy: any column ending in 'npi'
    for c in columns:
        if c.lower().endswith("npi"):
            return c
    return None


def _build_prompt(question: str, sql: str, columns: list[str], rows: list[dict]) -> str:
    # Limit to first 20 rows to keep prompt small
    sample = rows[:20]
    return (
        f"User question: {question}\n\n"
        f"SQL executed:\n```sql\n{sql}\n```\n\n"
        f"Columns: {columns}\n\n"
        f"Rows (up to 20):\n{json.dumps(sample, default=str, indent=2)}\n\n"
        "Emit the facts JSON now."
    )


def _safe_json_extract(text: str) -> dict:
    """Pull a top-level JSON object out of the LLM response, tolerating prose around it."""
    text = text.strip()
    # Quick path: response is already JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    # Tolerate ```json fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Last resort: greedy braces
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"facts": []}


def extract_facts(question: str, sql: str, columns: list[str], rows: list[dict]) -> list[dict]:
    """Synchronous extractor. Returns a list of {npi, category, fact_text} dicts.
    Safe to call with empty rows (returns []).
    """
    if not rows:
        return []
    npi_col = _find_npi_column(columns)
    if not npi_col:
        return []  # nothing to attribute facts to

    user = _build_prompt(question, sql, columns, rows)
    try:
        text = llm_chat(
            system=_SYSTEM,
            messages=[{"role": "user", "content": user}],
            max_tokens=1200,
        )
    except Exception as e:
        # Don't crash the query path because extraction failed
        print(f"[hcp_extractor] LLM call failed: {e}")
        return []

    parsed = _safe_json_extract(text)
    raw = parsed.get("facts", [])
    out = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        npi = str(f.get("npi", "")).strip()
        cat = f.get("category", "").strip()
        txt = (f.get("fact_text") or "").strip()
        if not npi or cat not in VALID_CATEGORIES or not txt:
            continue
        if len(txt) > 200:
            txt = txt[:197] + "..."
        out.append({"npi": npi, "category": cat, "fact_text": txt})
    return out


def extract_and_store(store: MemoryStore, question: str, sql: str,
                      columns: list[str], rows: list[dict],
                      source_query_id: Optional[int] = None) -> int:
    """Convenience: extract + bulk-insert. Returns number of facts written."""
    facts = extract_facts(question, sql, columns, rows)
    if not facts:
        return 0
    for f in facts:
        f["source_query_id"] = source_query_id
        f["fact_type"] = "AUTO"
    ids = store.add_hcp_facts_bulk(facts)
    return len(ids)
