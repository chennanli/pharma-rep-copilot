"""
FastAPI entry — two-pane HTMX UI for the demo.

Endpoints:
  GET  /                          → HTML page
  POST /ask                       → run agent, return result fragment (HTMX swap)
  POST /verify/{example_id}       → mark PENDING → VERIFIED (with optional edit_sql)
  POST /reject/{example_id}       → mark PENDING → REJECTED
  POST /note                      → write a NOTE-type HCP fact
  GET  /memory/examples           → fragment: list of recent verified examples
  GET  /memory/counts             → fragment: corpus + HCP counters
  GET  /memory/hcp/{npi}          → fragment: full fact list for one HCP

The page itself is `templates/index.html`. All updates are HTMX swaps.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .agent_graph import ask, step_names
from .hcp_extractor import extract_and_store
from .memory_store import get_store
from .sql_safety import validate as _validate_sql

load_dotenv()

BASE = Path(__file__).resolve().parent
app = FastAPI(title="Pharma Rep Copilot")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---------- helpers ----------

def _user_from(req: Request) -> str:
    """Demo-only: read the picked user from a cookie or fallback to default."""
    return req.cookies.get("demo_user") or os.getenv("DEMO_USER_DEFAULT", "alice")


def _run_extraction_background(question: str, sql: str, columns: list[str],
                               rows: list[dict], source_query_id: int):
    """Launch fact extraction without blocking the request."""
    async def _runner():
        loop = asyncio.get_event_loop()
        # extract_and_store is sync; offload to threadpool
        try:
            n = await loop.run_in_executor(
                None, extract_and_store,
                get_store(), question, sql, columns, rows, source_query_id,
            )
            print(f"[extract_hcp_facts] wrote {n} facts for query_id={source_query_id}")
        except Exception as e:
            print(f"[extract_hcp_facts] failed: {e}")
    asyncio.create_task(_runner())


# ---------- page ----------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    store = get_store()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "demo_user": _user_from(request),
            "steps": step_names(),
            "counts": store.count_examples(),
            "fact_count": store.count_facts(),
            "hcp_count": store.distinct_hcps(),
            "recent_examples": store.list_recent_verified(limit=5),
        },
    )


# ---------- ask + verify + reject ----------

@app.post("/ask", response_class=HTMLResponse)
async def post_ask(request: Request, question: str = Form(...)):
    """Run the agent. Persist as PENDING. Return result fragment for HTMX swap.
    Background-extract HCP facts on the way out."""
    user = _user_from(request)
    store = get_store()

    # Run agent (sync — wrap in threadpool to keep the FastAPI loop happy)
    loop = asyncio.get_event_loop()
    out = await loop.run_in_executor(None, ask, question, user)

    sr = out.get("safety_result")
    err = out.get("error")
    result = out.get("result")
    sql_text = sr.sql if (sr and sr.ok) else out.get("sql_raw", "")
    rowcount = result.rowcount if result else 0

    # Always create a PENDING row (even if exec failed) so the user can edit + verify
    example_id = store.add_pending(question=question, sql=sql_text or "-- (no sql)", author=user)

    # Background extraction (only if exec ok and we have rows)
    if (not err) and result and result.rows:
        _run_extraction_background(
            question=question, sql=sql_text,
            columns=result.columns, rows=result.rows,
            source_query_id=example_id,
        )

    return templates.TemplateResponse(
        "_result.html",
        {
            "request": request,
            "question": question,
            "sql": sql_text,
            "safety_ok": bool(sr and sr.ok),
            "safety_reason": (sr.reason if sr and not sr.ok else None) if sr else None,
            "warnings": list(sr.warnings) if sr else [],
            "error": err,
            "result_rows": (result.rows[:50] if result else []),
            "result_columns": (result.columns if result else []),
            "rowcount": rowcount,
            "elapsed_ms": (result.elapsed_ms if result else 0),
            "sources": out.get("sources", []),
            "examples_used": out.get("retrieved_examples", []),
            "hcp_facts_used": out.get("retrieved_hcp_facts", []),
            "example_id": example_id,
            "demo_user": user,
        },
    )


@app.post("/verify/{example_id}", response_class=HTMLResponse)
async def post_verify(request: Request, example_id: int,
                      edited_sql: Optional[str] = Form(None)):
    user = _user_from(request)
    store = get_store()
    clean_edit = edited_sql.strip() if edited_sql and edited_sql.strip() else None

    # A human-edited SQL becomes a VERIFIED few-shot example that steers future
    # generations — so it must clear the SAME safety gate as machine-generated SQL.
    # Never let an unsafe hand-edit into the corpus.
    if clean_edit is not None:
        sr = _validate_sql(clean_edit)
        if not sr.ok:
            raise HTTPException(
                400, f"Edited SQL rejected by the safety gate: {sr.reason}. Not verified.")
        clean_edit = sr.sql  # store the validated/normalized form

    try:
        store.mark_verified(example_id, author=user, edited_sql=clean_edit)
    except KeyError as e:
        raise HTTPException(404, f"example {example_id} not found") from e
    return await _memory_panel(request)


@app.post("/reject/{example_id}", response_class=HTMLResponse)
async def post_reject(request: Request, example_id: int):
    user = _user_from(request)
    store = get_store()
    store.mark_rejected(example_id, author=user)
    return await _memory_panel(request)


@app.post("/note", response_class=HTMLResponse)
async def post_note(request: Request,
                    npi: str = Form(...), category: str = Form(...),
                    fact_text: str = Form(...)):
    """Add a rep NOTE about an HCP."""
    user = _user_from(request)
    store = get_store()
    store.add_hcp_fact(npi=npi, category=category, fact_text=fact_text,
                       fact_type="NOTE", author=user)
    return await _memory_panel(request)


# ---------- memory panels ----------

@app.get("/memory/counts", response_class=HTMLResponse)
async def get_counts(request: Request):
    store = get_store()
    return templates.TemplateResponse(
        "_counts.html",
        {
            "request": request,
            "counts": store.count_examples(),
            "fact_count": store.count_facts(),
            "hcp_count": store.distinct_hcps(),
        },
    )


@app.get("/memory/examples", response_class=HTMLResponse)
async def get_examples(request: Request):
    store = get_store()
    return templates.TemplateResponse(
        "_examples_list.html",
        {"request": request, "recent_examples": store.list_recent_verified(limit=5)},
    )


@app.get("/memory/hcp/{npi}", response_class=HTMLResponse)
async def get_hcp(request: Request, npi: str):
    store = get_store()
    facts = store.facts_for_npi(npi)
    return templates.TemplateResponse(
        "_hcp_panel.html",
        {"request": request, "npi": npi, "facts": facts},
    )


async def _memory_panel(request: Request) -> HTMLResponse:
    """After verify/reject/note: refresh both the counts strip and the examples list
    in a single HTMX out-of-band swap response."""
    store = get_store()
    return templates.TemplateResponse(
        "_memory_combined.html",
        {
            "request": request,
            "counts": store.count_examples(),
            "fact_count": store.count_facts(),
            "hcp_count": store.distinct_hcps(),
            "recent_examples": store.list_recent_verified(limit=5),
        },
    )


# ---------- healthcheck ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}
