"""Tests for the two-layer memory store.

Exercises both layers (Question Memory + Doctor Memory) using a real
SQLite + Chroma backing store in a temp directory. Chroma downloads its
default ONNX embedding model on first use (~80MB); CI caches this between
runs.
"""
from __future__ import annotations

from app.memory_store import MemoryStore

# ---------- Question Memory (sql_examples) ----------

def test_add_pending_returns_id(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    eid = store.add_pending("Top oncologists in CA", "SELECT 1", author="alice")
    assert isinstance(eid, int)
    assert eid > 0


def test_count_examples_reflects_pending_and_verified(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    e1 = store.add_pending("Q1", "SELECT 1", author="alice")
    store.add_pending("Q2", "SELECT 2", author="alice")
    counts = store.count_examples()
    assert counts["PENDING"] == 2
    assert counts["VERIFIED"] == 0
    assert counts["REJECTED"] == 0

    store.mark_verified(e1, author="alice")
    counts = store.count_examples()
    assert counts["PENDING"] == 1
    assert counts["VERIFIED"] == 1


def test_mark_verified_records_edit(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    eid = store.add_pending("Q", "SELECT 1", author="alice")
    verified = store.mark_verified(
        eid, author="alice", edited_sql="SELECT 2 LIMIT 10"
    )
    assert verified.status == "VERIFIED"
    assert verified.sql == "SELECT 2 LIMIT 10"
    # original SQL stashed in edited_sql field for audit
    assert verified.edited_sql == "SELECT 1"


def test_recall_examples_returns_verified_only(tmp_memory_paths):
    """Recall must only surface VERIFIED examples — never PENDING or REJECTED."""
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    e1 = store.add_pending(
        "Top oncologists in CA prescribing Herceptin",
        "SELECT * FROM partd.prescriber_drug_yearly",
        author="alice",
    )
    store.add_pending(
        "Top cardiologists prescribing statins",
        "SELECT * FROM partd.prescriber_drug_yearly",
        author="alice",
    )
    # Only mark the first one verified
    store.mark_verified(e1, author="alice")

    hits = store.recall_examples(
        "Top oncologists in California prescribing Kadcyla", k=5
    )
    # Should retrieve 1 hit (the verified Herceptin one), not 2
    assert len(hits) == 1
    assert "Herceptin" in hits[0]["question"]


def test_recall_examples_empty_on_cold_start(tmp_memory_paths):
    """With nothing verified, recall must return an empty list (not crash)."""
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    hits = store.recall_examples("any question", k=3)
    assert hits == []


def test_mark_rejected_removes_from_chroma(tmp_memory_paths):
    """A rejected example should NOT show up in subsequent recall_examples calls."""
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    eid = store.add_pending("Q", "SELECT 1", author="alice")
    store.mark_verified(eid, author="alice")
    # Verify it's recallable first
    assert len(store.recall_examples("Q", k=3)) == 1

    store.mark_rejected(eid, author="alice")
    # After reject, recall should miss it
    assert len(store.recall_examples("Q", k=3)) == 0


# ---------- Doctor Memory (hcp_facts) ----------

def test_add_hcp_fact_then_facts_for_npi_roundtrip(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    npi = "1234567890"
    store.add_hcp_fact(npi, "clinical_focus", "Oncologist in SF",
                       fact_type="AUTO")
    store.add_hcp_fact(npi, "rep_note", "Interested in subQ formulation",
                       fact_type="NOTE", author="alice")
    facts = store.facts_for_npi(npi)
    assert len(facts) == 2
    categories = {f.category for f in facts}
    assert categories == {"clinical_focus", "rep_note"}


def test_recall_hcp_facts_filters_by_npi(tmp_memory_paths):
    """When npis filter is given, only facts about those NPIs should come back."""
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    store.add_hcp_fact("1111111111", "clinical_focus",
                       "Oncologist in SF", fact_type="AUTO")
    store.add_hcp_fact("2222222222", "clinical_focus",
                       "Cardiologist in LA", fact_type="AUTO")

    # Filter to first NPI only
    hits = store.recall_hcp_facts("oncology", npis=["1111111111"])
    assert all(h["npi"] == "1111111111" for h in hits)
    assert len(hits) >= 1


def test_count_facts_and_distinct_hcps(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    store.add_hcp_fact("1111111111", "clinical_focus", "fact A")
    store.add_hcp_fact("1111111111", "prescribing_pattern", "fact B")
    store.add_hcp_fact("2222222222", "clinical_focus", "fact C")
    assert store.count_facts() == 3
    assert store.distinct_hcps() == 2


def test_add_hcp_facts_bulk(tmp_memory_paths):
    store = MemoryStore(
        db_path=tmp_memory_paths["db_path"],
        chroma_dir=tmp_memory_paths["chroma_dir"],
    )
    facts = [
        {"npi": "1111111111", "category": "clinical_focus",
         "fact_text": "Oncologist", "source_query_id": 1},
        {"npi": "1111111111", "category": "prescribing_pattern",
         "fact_text": "Top Herceptin prescriber"},
    ]
    ids = store.add_hcp_facts_bulk(facts)
    assert len(ids) == 2
    assert store.count_facts() == 2
