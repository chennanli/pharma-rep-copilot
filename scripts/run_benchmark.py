"""
Run the golden 30-question benchmark.

For each question:
  1. Invoke the agent.
  2. Score recall / exec / kw.
  3. Emit a row to a `run-*.jsonl`.

Final output: scorecard printed to stdout + jsonl path.

Usage:
    python scripts/run_benchmark.py
    python scripts/run_benchmark.py --questions benchmarks/questions.jsonl --out benchmarks/run-XYZ.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Make `app.*` importable when running as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent_graph import ask  # noqa: E402


def score_question(q: dict, agent_out: dict) -> dict:
    """Compute recall, exec, kw for one question."""
    sr = agent_out.get("safety_result")
    sql = (sr.sql if sr and sr.ok else (agent_out.get("sql_raw") or "")).lower()
    error = agent_out.get("error")
    result = agent_out.get("result")

    expected_tables = [t.lower() for t in q.get("expected_tables", [])]
    expected_keywords = [k.lower() for k in q.get("expected_keywords", [])]

    recall = all(t in sql for t in expected_tables) if expected_tables else None
    exec_ok = (error is None and result is not None)

    kw = None
    if exec_ok and expected_keywords and result and result.rows:
        # Look at SQL + result column names + first row text rep
        haystack = sql + " " + " ".join(result.columns) + " " + json.dumps(result.rows[:5], default=str).lower()
        kw = any(k in haystack for k in expected_keywords)

    rek = bool(recall) and bool(exec_ok) and bool(kw)

    return {
        "id": q["id"],
        "category": q["category"],
        "difficulty": q["difficulty"],
        "recall": recall,
        "exec": exec_ok,
        "kw": kw,
        "rek": rek,
        "rowcount": result.rowcount if result else 0,
        "elapsed_ms": result.elapsed_ms if result else 0,
        "error": error,
        "sql_preview": sql[:240],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--questions", default="benchmarks/questions.jsonl")
    p.add_argument("--out", default=None)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    out_path = args.out or f"benchmarks/run-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.jsonl"
    out_fh = open(out_path, "w")

    rows = []
    with open(args.questions) as fh:
        questions = [json.loads(line) for line in fh if line.strip()]
    if args.limit:
        questions = questions[: args.limit]

    print(f"Running {len(questions)} questions → {out_path}")
    for i, q in enumerate(questions, 1):
        t0 = time.time()
        try:
            agent_out = ask(q["question"])
        except Exception as e:
            agent_out = {"error": f"agent_exception: {e}"}
        elapsed = time.time() - t0
        scored = score_question(q, agent_out)
        scored["wallclock_s"] = round(elapsed, 1)
        rows.append(scored)
        out_fh.write(json.dumps(scored, default=str) + "\n")
        out_fh.flush()
        flag = "✓" if scored["rek"] else ("⚠" if scored["exec"] else "✗")
        print(f"  [{i:2d}/{len(questions)}] {flag} {q['id']:<10} rec={scored['recall']} exec={scored['exec']} kw={scored['kw']}  ({elapsed:.1f}s)")

    out_fh.close()

    # Scorecard
    n = len(rows)
    def pct(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return (sum(1 for v in vals if v) / len(vals) * 100) if vals else 0.0

    print("\n=== Scorecard ===")
    print(f"  N questions : {n}")
    print(f"  recall      : {pct('recall'):.0f}%")
    print(f"  exec        : {pct('exec'):.0f}%")
    print(f"  kw          : {pct('kw'):.0f}%")
    print(f"  REK         : {pct('rek'):.0f}%")
    print(f"  output      : {out_path}")


if __name__ == "__main__":
    main()
