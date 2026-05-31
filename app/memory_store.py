"""
Two team-shared memory stores.

Both serve the same end users (the reps using the chat UI). Both grow automatically
as the team uses the tool. They remember different things:

  Question Memory — verified question + SQL pairs. Helps future similar questions get
                    answered correctly without re-discovering the same SQL pattern.
                    SQLite table: sql_examples. Chroma collection: sql_examples.

  Doctor Memory  — per-HCP atomic facts (auto-extracted from query results + free-text
                    rep notes). Helps reps walk into calls with accumulated team context
                    on individual physicians.
                    SQLite table: hcp_facts. Chroma collection: hcp_memory.

Storage rationale:
  - SQLite (MEMORY_DB_PATH) — structured source of truth, easy to SELECT * for audit.
  - Chroma  (CHROMA_DIR)    — vector index for similarity retrieval at query time.

Writes go to SQLite first, then mirror to Chroma synchronously so the very next query
can already use the new memory entry.
"""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

import chromadb
from chromadb.config import Settings
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path(os.getenv("MEMORY_DB_PATH", "data/memory.db"))
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", "data/chroma"))

# 5 enumerated categories for HCP facts. Keep in sync with the spec §4.3 and the deck slide 8.
HcpCategory = Literal[
    "clinical_focus",
    "prescribing_pattern",
    "payment_history",
    "rep_note",
    "affiliation",
]

ExampleStatus = Literal["PENDING", "VERIFIED", "REJECTED"]


# ---------- dataclasses returned to callers ----------

@dataclass
class SqlExample:
    id: int
    question: str
    sql: str
    status: ExampleStatus
    author: Optional[str]
    edited_sql: Optional[str]
    ts: int

@dataclass
class HcpFact:
    id: int
    npi: str
    category: HcpCategory
    fact_text: str
    fact_type: Literal["AUTO", "NOTE"]
    source_query_id: Optional[int]
    author: Optional[str]
    ts: int


# ---------- DDL ----------

_DDL = """
CREATE TABLE IF NOT EXISTS sql_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    question    TEXT NOT NULL,
    sql         TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('PENDING','VERIFIED','REJECTED')),
    author      TEXT,
    edited_sql  TEXT,
    ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sql_examples_status ON sql_examples(status);

CREATE TABLE IF NOT EXISTS hcp_facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    npi             TEXT NOT NULL,
    category        TEXT NOT NULL,
    fact_text       TEXT NOT NULL,
    fact_type       TEXT NOT NULL CHECK (fact_type IN ('AUTO','NOTE')),
    source_query_id INTEGER,
    author          TEXT,
    ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hcp_facts_npi ON hcp_facts(npi);
CREATE INDEX IF NOT EXISTS idx_hcp_facts_cat ON hcp_facts(category);
"""


# ---------- main class ----------

class MemoryStore:
    """All reads + writes for both memory layers go through here.

    One instance per process; FastAPI app holds a module-level singleton via get_store().
    """

    def __init__(self, db_path: Path = DB_PATH, chroma_dir: Path = CHROMA_DIR):
        self.db_path = db_path
        self.chroma_dir = chroma_dir
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._conn.commit()

        # Chroma in persistent mode; uses default embedding (onnx all-MiniLM-L6-v2).
        self._chroma = chromadb.PersistentClient(
            path=str(self.chroma_dir),
            settings=Settings(anonymized_telemetry=False),
        )
        self._coll_examples = self._chroma.get_or_create_collection(
            name="sql_examples",
            metadata={"hnsw:space": "cosine"},
        )
        self._coll_hcp = self._chroma.get_or_create_collection(
            name="hcp_memory",
            metadata={"hnsw:space": "cosine"},
        )

    # ---------- Question Memory (sql_examples) ----------

    def add_pending(self, question: str, sql: str, author: Optional[str] = None) -> int:
        """Insert a new PENDING example. Returns the row id.

        Called after every successful exec — gives admin/user something to verify later.
        """
        now = int(time.time() * 1000)
        cur = self._conn.execute(
            "INSERT INTO sql_examples (question, sql, status, author, ts) VALUES (?, ?, 'PENDING', ?, ?)",
            (question, sql, author, now),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def mark_verified(self, example_id: int, author: Optional[str] = None,
                      edited_sql: Optional[str] = None) -> SqlExample:
        """Flip a PENDING example to VERIFIED. If `edited_sql` is given, the edited form
        becomes the canonical `sql` and the original is stashed in `edited_sql` for audit.
        """
        # Read current row
        row = self._conn.execute(
            "SELECT * FROM sql_examples WHERE id = ?", (example_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"sql_examples id={example_id} not found")

        new_sql = edited_sql if edited_sql is not None else row["sql"]
        original_sql_stash = row["sql"] if edited_sql is not None else None

        self._conn.execute(
            "UPDATE sql_examples SET status='VERIFIED', sql=?, edited_sql=?, author=? WHERE id=?",
            (new_sql, original_sql_stash, author or row["author"], example_id),
        )
        self._conn.commit()

        # Sync to Chroma — embed the question (we retrieve by question similarity)
        self._coll_examples.upsert(
            ids=[f"ex-{example_id}"],
            documents=[row["question"]],
            metadatas=[{
                "sql": new_sql,
                "author": author or row["author"] or "",
                "ts": int(row["ts"]),
            }],
        )
        return self._row_to_example(row, override={
            "status": "VERIFIED", "sql": new_sql, "edited_sql": original_sql_stash,
            "author": author or row["author"],
        })

    def mark_rejected(self, example_id: int, author: Optional[str] = None) -> None:
        """Reject an example. Removes it from Chroma so it never shows up as few-shot again.
        Row stays in SQLite for audit.
        """
        self._conn.execute(
            "UPDATE sql_examples SET status='REJECTED', author=? WHERE id=?",
            (author, example_id),
        )
        self._conn.commit()
        try:
            self._coll_examples.delete(ids=[f"ex-{example_id}"])
        except Exception:
            pass  # silent — Chroma will 404 if it was never added

    def recall_examples(self, question: str, k: int = 3) -> list[dict]:
        """Top-K verified Q→SQL by similarity. Returns list of {id, question, sql, score, author, ts}.
        Empty list on cold start.
        """
        if self._coll_examples.count() == 0:
            return []
        res = self._coll_examples.query(
            query_texts=[question],
            n_results=min(k, self._coll_examples.count()),
        )
        out = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for id_, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            # cosine distance → similarity ≈ 1 - dist
            score = max(0.0, 1.0 - float(dist))
            out.append({
                "id": id_,
                "question": doc,
                "sql": meta.get("sql", ""),
                "author": meta.get("author") or "",
                "ts": int(meta.get("ts") or 0),
                "score": score,
            })
        return out

    def list_recent_verified(self, limit: int = 10) -> list[SqlExample]:
        rows = self._conn.execute(
            "SELECT * FROM sql_examples WHERE status='VERIFIED' ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._row_to_example(r) for r in rows]

    def count_examples(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM sql_examples GROUP BY status"
        ).fetchall()
        d = {r["status"]: int(r["n"]) for r in rows}
        d.setdefault("VERIFIED", 0)
        d.setdefault("PENDING", 0)
        d.setdefault("REJECTED", 0)
        return d

    # ---------- Doctor Memory (hcp_facts) ----------

    def add_hcp_fact(self, npi: str, category: HcpCategory, fact_text: str,
                     fact_type: Literal["AUTO", "NOTE"] = "AUTO",
                     source_query_id: Optional[int] = None,
                     author: Optional[str] = None) -> int:
        """Insert one HCP fact. Returns row id. Also embeds into Chroma immediately."""
        now = int(time.time() * 1000)
        cur = self._conn.execute(
            "INSERT INTO hcp_facts (npi, category, fact_text, fact_type, source_query_id, author, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(npi), category, fact_text, fact_type, source_query_id, author, now),
        )
        self._conn.commit()
        fact_id = int(cur.lastrowid)

        self._coll_hcp.upsert(
            ids=[f"hcp-{fact_id}"],
            documents=[fact_text],
            metadatas=[{
                "npi": str(npi),
                "category": category,
                "fact_type": fact_type,
                "source_query_id": source_query_id or 0,
                "author": author or "",
                "ts": now,
            }],
        )
        return fact_id

    def add_hcp_facts_bulk(self, facts: Iterable[dict]) -> list[int]:
        """Convenience for hcp_extractor: each dict has keys
        (npi, category, fact_text, [fact_type, source_query_id, author]).
        """
        ids = []
        for f in facts:
            ids.append(self.add_hcp_fact(
                npi=f["npi"],
                category=f["category"],
                fact_text=f["fact_text"],
                fact_type=f.get("fact_type", "AUTO"),
                source_query_id=f.get("source_query_id"),
                author=f.get("author"),
            ))
        return ids

    def facts_for_npi(self, npi: str) -> list[HcpFact]:
        rows = self._conn.execute(
            "SELECT * FROM hcp_facts WHERE npi=? ORDER BY ts DESC", (str(npi),)
        ).fetchall()
        return [self._row_to_fact(r) for r in rows]

    def recall_hcp_facts(self, query_text: str, npis: Optional[list[str]] = None,
                         k: int = 8) -> list[dict]:
        """Vector-retrieve HCP facts relevant to a query. If `npis` is given, filter to those.

        Returns list of {id, npi, category, fact_text, author, ts, score}.
        """
        if self._coll_hcp.count() == 0:
            return []
        where = {"npi": {"$in": [str(n) for n in npis]}} if npis else None
        res = self._coll_hcp.query(
            query_texts=[query_text],
            n_results=min(k, self._coll_hcp.count()),
            where=where,
        )
        out = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for id_, doc, meta, dist in zip(ids, docs, metas, dists, strict=False):
            score = max(0.0, 1.0 - float(dist))
            out.append({
                "id": id_,
                "npi": meta.get("npi"),
                "category": meta.get("category"),
                "fact_text": doc,
                "author": meta.get("author") or "",
                "ts": int(meta.get("ts") or 0),
                "score": score,
            })
        return out

    def count_facts(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM hcp_facts").fetchone()
        return int(row["n"])

    def distinct_hcps(self) -> int:
        row = self._conn.execute("SELECT COUNT(DISTINCT npi) AS n FROM hcp_facts").fetchone()
        return int(row["n"])

    # ---------- helpers ----------

    def _row_to_example(self, row: sqlite3.Row, override: Optional[dict] = None) -> SqlExample:
        d = dict(row) if row is not None else {}
        if override:
            d.update(override)
        return SqlExample(
            id=int(d["id"]),
            question=d["question"],
            sql=d["sql"],
            status=d["status"],
            author=d.get("author"),
            edited_sql=d.get("edited_sql"),
            ts=int(d["ts"]),
        )

    def _row_to_fact(self, row: sqlite3.Row) -> HcpFact:
        return HcpFact(
            id=int(row["id"]),
            npi=row["npi"],
            category=row["category"],
            fact_text=row["fact_text"],
            fact_type=row["fact_type"],
            source_query_id=row["source_query_id"],
            author=row["author"],
            ts=int(row["ts"]),
        )


# ---------- module singleton ----------

_singleton: Optional[MemoryStore] = None

def get_store() -> MemoryStore:
    global _singleton
    if _singleton is None:
        _singleton = MemoryStore()
    return _singleton


# ---------- self-test ----------

if __name__ == "__main__":
    # Smoke test: create a memory store in a temp dir, write/read both layers.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["MEMORY_DB_PATH"] = str(tmp / "m.db")
        os.environ["CHROMA_DIR"] = str(tmp / "chroma")
        store = MemoryStore(db_path=tmp / "m.db", chroma_dir=tmp / "chroma")

        eid = store.add_pending("Top oncologists in CA prescribing Herceptin in 2023",
                                "SELECT 1", author="alice")
        store.mark_verified(eid, author="alice",
                            edited_sql="SELECT n.npi FROM partd.prescriber_drug_yearly p "
                                       "JOIN reference.drug_alias d ON d.brand='HERCEPTIN' "
                                       "WHERE p.drug_name=d.generic LIMIT 10")
        hits = store.recall_examples("Top oncologists in California prescribing Kadcyla", k=3)
        assert len(hits) == 1, hits
        print(f"[OK] recall_examples → 1 hit, score={hits[0]['score']:.2f}")

        store.add_hcp_fact("1234567890", "prescribing_pattern",
                           "Top-decile trastuzumab prescriber in CA 2023, 47 claims",
                           fact_type="AUTO", source_query_id=eid)
        store.add_hcp_fact("1234567890", "rep_note",
                           "Mentioned interest in subQ Herceptin formulation at ASCO 2024",
                           fact_type="NOTE", author="alice")
        facts = store.facts_for_npi("1234567890")
        assert len(facts) == 2
        print("[OK] facts_for_npi → 2 facts")

        hcp_hits = store.recall_hcp_facts("HER2 breast oncology Roche", npis=["1234567890"])
        assert len(hcp_hits) > 0
        print(f"[OK] recall_hcp_facts → {len(hcp_hits)} hits")

        print(f"counts: examples={store.count_examples()}, facts={store.count_facts()}")
