"""
CLI entry point.

Usage:
    python -m app.cli "How many oncologists in California prescribed Herceptin in 2022?"
"""
from __future__ import annotations

import json
import sys

from .agent_graph import ask


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m app.cli <question>", file=sys.stderr)
        sys.exit(2)

    question = " ".join(sys.argv[1:])
    out = ask(question)

    print("=== Question ===")
    print(question)
    print()
    print("=== SQL ===")
    sr = out.get("safety_result")
    print(sr.sql if sr and sr.ok else out.get("sql_raw", "<none>"))
    print()

    if sr and not sr.ok:
        print(f"=== Safety REJECT ===\n{sr.reason}")
        sys.exit(1)

    if out.get("error"):
        print(f"=== Execution ERROR ===\n{out['error']}")
        sys.exit(1)

    res = out["result"]
    print(f"=== Result ({res.rowcount} rows in {res.elapsed_ms} ms) ===")
    if res.rows:
        print(json.dumps(res.rows[:10], indent=2, default=str))
        if res.rowcount > 10:
            print(f"... {res.rowcount - 10} more rows.")
    print()
    print("=== Sources ===")
    for s in out.get("sources", []):
        print(f"- {s['dataset']}: {s['url']}")


if __name__ == "__main__":
    main()
