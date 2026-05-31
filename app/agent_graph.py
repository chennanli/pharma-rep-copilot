"""
LangGraph agent with team memory — S1 (2026-05-29).

8 nodes:
  rewrite
    → retrieve_schema
    → retrieve_examples       (NEW: Question Memory read)
    → retrieve_hcp_context    (NEW: Doctor Memory read)
    → gen_sql
    → validate                (sqlglot AST gate — existing)
    → exec                    (read-only Postgres — existing)
    → extract_hcp_facts       (NEW: Doctor Memory background write)

The graph stays synchronous end-to-end EXCEPT extract_hcp_facts, which is launched as
asyncio.create_task by the FastAPI handler after the graph returns — see app/main.py.
The node here just records what *would* be extracted; the handler does the actual write.
This keeps agent latency unaffected by extraction.
"""
from __future__ import annotations

import re
from typing import TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph

from . import prompts
from .llm_client import chat as llm_chat
from .memory_store import get_store
from .pg_runner import QueryResult, execute_select
from .schema_introspect import schema_summary_markdown
from .sql_safety import SafetyResult, validate

load_dotenv()


class State(TypedDict, total=False):
    question: str
    user: str | None
    question_rewritten: str
    schema_summary: str
    retrieved_examples: list[dict]
    retrieved_hcp_facts: list[dict]
    sql_raw: str
    sql_validated: str
    safety_result: SafetyResult
    result: QueryResult | None
    error: str | None
    sources: list[dict]
    # populated by exec node so the FastAPI handler can schedule background extraction
    extraction_payload: dict | None


# ---------- nodes ----------

def node_rewrite(state: State) -> dict:
    """Light normalization. Demo doesn't need a heavy rewrite step yet."""
    q = state["question"].strip()
    return {"question_rewritten": q}


def node_retrieve_schema(state: State) -> dict:
    """Dump full live schema. For ~10 tables this is ~3K tokens."""
    return {"schema_summary": schema_summary_markdown()}


def node_retrieve_examples(state: State) -> dict:
    """Question Memory read: top-K verified Q→SQL pairs from Chroma."""
    store = get_store()
    hits = store.recall_examples(state["question_rewritten"], k=3)
    return {"retrieved_examples": hits}


def node_retrieve_hcp_context(state: State) -> dict:
    """Doctor Memory read: pull facts that match the question.

    For S1 we do a simple text-similarity recall (no NPI extraction from the question yet).
    If the user explicitly names an HCP, recall will surface their facts via embedding
    similarity. Per-NPI filter from prior turn could be added later.
    """
    store = get_store()
    hits = store.recall_hcp_facts(state["question_rewritten"], k=8)
    return {"retrieved_hcp_facts": hits}


def node_gen_sql(state: State) -> dict:
    msgs, system = prompts.build_messages(
        state["question_rewritten"],
        state["schema_summary"],
        examples=state.get("retrieved_examples", []),
        hcp_facts=state.get("retrieved_hcp_facts", []),
    )
    text = llm_chat(system=system, messages=msgs, max_tokens=1500)
    sql = _extract_sql(text)
    return {"sql_raw": sql}


def node_validate(state: State) -> dict:
    sr = validate(state["sql_raw"])
    return {"safety_result": sr, "sql_validated": sr.sql if sr.ok else state["sql_raw"]}


def node_exec(state: State) -> dict:
    sr: SafetyResult = state["safety_result"]
    if not sr.ok:
        return {"result": None, "error": f"safety_reject: {sr.reason}", "extraction_payload": None}
    try:
        result = execute_select(sr.sql)
        sources = _infer_sources(sr.sql)
        # Stash the data the extraction step will need; actual extraction is run by the
        # FastAPI layer as a background task to keep the user-perceived latency low.
        payload = {
            "question": state["question_rewritten"],
            "sql": sr.sql,
            "columns": result.columns,
            "rows": result.rows,
        }
        return {"result": result, "error": None, "sources": sources, "extraction_payload": payload}
    except Exception as e:
        return {"result": None, "error": f"exec_error: {e}", "extraction_payload": None}


def node_extract_hcp_facts(state: State) -> dict:
    """Symbolic node — actual extraction runs in the FastAPI handler's background task.

    The node exists so the graph shape matches the 8-step diagram. Returns a no-op
    pass-through of `sources` because newer LangGraph rejects nodes that return {}.
    """
    return {"sources": state.get("sources", [])}


# ---------- helpers ----------

def _extract_sql(text: str) -> str:
    """Pull a ```sql block out of the LLM output; fall back to the whole text."""
    m = re.search(r"```sql\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text.strip()


def _infer_sources(sql: str) -> list[dict]:
    """Coarsely tag which datasets were touched, for MLR-style citation."""
    lower = sql.lower()
    sources = []
    if "payments." in lower:
        sources.append({"dataset": "CMS Open Payments", "url": "https://openpaymentsdata.cms.gov/"})
    if "partd." in lower:
        sources.append({"dataset": "Medicare Part D Prescriber",
                        "url": "https://data.cms.gov/provider-summary-by-type-of-service/medicare-part-d-prescribers"})
    if "npi." in lower:
        sources.append({"dataset": "NPI Registry", "url": "https://npiregistry.cms.hhs.gov/"})
    if "reference.drug_alias" in lower:
        sources.append({"dataset": "Brand↔generic seed (local)", "url": "scripts/seed_drug_alias.py"})
    return sources


# ---------- graph ----------

def build_graph():
    g = StateGraph(State)
    g.add_node("rewrite", node_rewrite)
    g.add_node("retrieve_schema", node_retrieve_schema)
    g.add_node("retrieve_examples", node_retrieve_examples)
    g.add_node("retrieve_hcp_context", node_retrieve_hcp_context)
    g.add_node("gen_sql", node_gen_sql)
    g.add_node("validate", node_validate)
    g.add_node("exec", node_exec)
    g.add_node("extract_hcp_facts", node_extract_hcp_facts)

    g.add_edge(START, "rewrite")
    g.add_edge("rewrite", "retrieve_schema")
    g.add_edge("retrieve_schema", "retrieve_examples")
    g.add_edge("retrieve_examples", "retrieve_hcp_context")
    g.add_edge("retrieve_hcp_context", "gen_sql")
    g.add_edge("gen_sql", "validate")
    g.add_edge("validate", "exec")
    g.add_edge("exec", "extract_hcp_facts")
    g.add_edge("extract_hcp_facts", END)
    return g.compile()


# Module-level compiled graph (single instance shared across requests)
_GRAPH = None

def _graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    return _GRAPH


def ask(question: str, user: str | None = None) -> dict:
    """Synchronous one-shot. cli.py uses this. FastAPI uses astream()."""
    return _graph().invoke({"question": question, "user": user})


def step_names() -> list[str]:
    """Names of nodes in execution order — used by the UI to render the progress bar."""
    return [
        "rewrite", "retrieve_schema", "retrieve_examples", "retrieve_hcp_context",
        "gen_sql", "validate", "exec", "extract_hcp_facts",
    ]
