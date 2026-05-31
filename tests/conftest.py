"""Shared pytest fixtures."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the project root importable so `from app.X import Y` works in tests
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def tmp_memory_paths(tmp_path, monkeypatch):
    """Point MemoryStore at a fresh temp dir for the test, and isolate env vars."""
    db_path = tmp_path / "memory.db"
    chroma_dir = tmp_path / "chroma"
    monkeypatch.setenv("MEMORY_DB_PATH", str(db_path))
    monkeypatch.setenv("CHROMA_DIR", str(chroma_dir))
    # Reset the module-level singleton so a fresh test gets a fresh store
    import app.memory_store as ms
    ms._singleton = None
    yield {"db_path": db_path, "chroma_dir": chroma_dir}
    ms._singleton = None
