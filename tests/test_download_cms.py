"""No-network regression tests for the CMS downloader's dedup logic.

The full `make demo-real` path hits the network; here we only test the pure
de-duplication helper that guarantees the broad CA pull + the hero-drug top-up
don't produce duplicate (NPI, brand, generic) rows.
"""
from __future__ import annotations

import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "download_cms_via_api.py"
_spec = importlib.util.spec_from_file_location("dlcms", _SCRIPT)
dlcms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dlcms)


def test_dedup_part_d_rows_removes_case_insensitive_duplicates():
    rows = [
        {"Prscrbr_NPI": "1", "Brnd_Name": "HERCEPTIN", "Gnrc_Name": "Trastuzumab", "tot_clms": "10"},
        # same (npi, brand, generic) but different casing + a different metric: still a dup
        {"Prscrbr_NPI": "1", "Brnd_Name": "Herceptin", "Gnrc_Name": "trastuzumab", "tot_clms": "11"},
        {"Prscrbr_NPI": "2", "Brnd_Name": "KADCYLA", "Gnrc_Name": "Ado-Trastuzumab Emtansine", "tot_clms": "5"},
    ]
    out = list(dlcms.dedup_part_d_rows(iter(rows)))
    assert len(out) == 2
    assert {r["Prscrbr_NPI"] for r in out} == {"1", "2"}
    # order preserved; first occurrence kept
    assert out[0]["tot_clms"] == "10"


def test_dedup_part_d_rows_keeps_distinct_drugs_for_same_npi():
    rows = [
        {"Prscrbr_NPI": "9", "Brnd_Name": "HERCEPTIN", "Gnrc_Name": "Trastuzumab"},
        {"Prscrbr_NPI": "9", "Brnd_Name": "KADCYLA", "Gnrc_Name": "Ado-Trastuzumab Emtansine"},
    ]
    out = list(dlcms.dedup_part_d_rows(iter(rows)))
    assert len(out) == 2


def test_hero_drugs_include_the_demo_drugs():
    names = {d.upper() for d in dlcms.PART_D_HERO_DRUGS}
    assert "TRASTUZUMAB" in names          # Herceptin
    assert "ADO-TRASTUZUMAB EMTANSINE" in names  # Kadcyla


# ---------- fail-closed validation paths ----------

import pytest  # noqa: E402


def test_missing_hero_drugs_uses_exact_match_not_substring():
    # All three present → nothing missing.
    assert dlcms.missing_hero_drugs(
        {"TRASTUZUMAB", "ADO-TRASTUZUMAB EMTANSINE", "PEMBROLIZUMAB"}) == []
    # Herceptin (TRASTUZUMAB) absent, but Kadcyla present: a substring check would
    # wrongly pass; exact match must still report TRASTUZUMAB missing.
    missing = dlcms.missing_hero_drugs({"ADO-TRASTUZUMAB EMTANSINE", "PEMBROLIZUMAB"})
    assert "TRASTUZUMAB" in missing


class _FakeCur:
    def __init__(self, counts):
        self.counts = counts
        self._g = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._g = params[1] if params and len(params) > 1 else None

    def fetchone(self):
        return (self.counts.get(self._g, 0),)


class _FakeConn:
    def __init__(self, counts):
        self.counts = counts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCur(self.counts)


def test_truncate_payments_fails_closed_on_db_error(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(dlcms, "_pg_conn", boom)
    with pytest.raises(RuntimeError):
        dlcms._truncate_payments()


def test_post_load_check_fails_when_a_hero_drug_missing(monkeypatch):
    # Herceptin/TRASTUZUMAB has 0 rows → must raise.
    counts = {"TRASTUZUMAB": 0, "ADO-TRASTUZUMAB EMTANSINE": 5, "PEMBROLIZUMAB": 9}
    monkeypatch.setattr(dlcms, "_pg_conn", lambda: _FakeConn(counts))
    with pytest.raises(RuntimeError):
        dlcms._post_load_check("CA")


def test_post_load_check_passes_when_all_present(monkeypatch):
    counts = {"TRASTUZUMAB": 6, "ADO-TRASTUZUMAB EMTANSINE": 1, "PEMBROLIZUMAB": 63}
    monkeypatch.setattr(dlcms, "_pg_conn", lambda: _FakeConn(counts))
    dlcms._post_load_check("CA")  # should not raise
