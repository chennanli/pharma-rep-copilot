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
