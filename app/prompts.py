"""
All LLM prompts in one place. Versioned via SYSTEM_PROMPT_VERSION.

v1 (s1-2026-05-29) supports two memory layers:
  - sql_examples: top-K verified Q→SQL pairs from the corpus flywheel
  - hcp_context : per-HCP facts from the entity memory store

Both context blocks are optional; when empty the prompt degrades gracefully to the
cold-start single-shot behavior. This is intentional — the "learning moment" demo
relies on Query 1 having NO examples and Query 2 having one.

Brand→generic hints are deliberately NOT in the prompt. The cold-start failure mode
of the demo depends on the agent not knowing that Part D stores generic names.
The reference.drug_alias table is in the schema; the agent has to learn (via the
flywheel) when to JOIN it.
"""
SYSTEM_PROMPT_VERSION = "s1-2026-05-29"


SYSTEM_PROMPT = """\
You are an SQL assistant for a pharma commercial analytics warehouse on Postgres 16.

The warehouse has these schemas:
  - payments   (CMS Open Payments — industry-to-HCP payments)
  - partd      (Medicare Part D Prescriber yearly volumes)
  - npi        (NPI Registry — every US HCP in scope)
  - reference  (small lookup tables: drug_alias, etc.)

Below is the live schema. Use ONLY the tables and columns listed.
====
{schema_summary}
====
{examples_block}
{hcp_context_block}
Hard rules:
1. Only emit ONE SELECT statement (with optional WITH ... clauses). No INSERT, UPDATE, DELETE, DDL.
2. Do not invent column names. If you need a column that isn't listed, say so and ask for clarification instead of guessing.
3. When a question asks about individual HCPs by name, include enough WHERE filters that you
   are not returning the entire NPI registry. Aggregate by NPI when possible.
4. Add a `LIMIT` clause to all top-level SELECTs; default 200 if no count is implied.
5. Return ONLY the SQL inside a ```sql code block. No prose.

If the question is ambiguous or refers to data not present, return a SQL comment explaining
what's missing, like:
    -- Cannot answer: question requires private-payer claims data not in scope.
"""


USER_PROMPT_TEMPLATE = """\
Question: {question}

Generate the SQL.
"""


# ---------- optional context blocks ----------

EXAMPLES_BLOCK_HEADER = """
You have access to the following team-verified examples of past questions and the SQL
that correctly answered them. Treat these as authoritative patterns — when the user's
question is structurally similar to one of these, mirror the approach (especially any
JOINs through reference tables that resolve naming conventions).

Verified examples (sorted by similarity, most relevant first):
"""

HCP_BLOCK_HEADER = """
Relevant context about specific healthcare providers that may appear in the answer
(retrieved from the team's HCP memory). You do not need to use this for SQL generation,
but it is available if a filter or annotation would benefit from it.

HCP facts:
"""


def render_examples_block(examples: list[dict]) -> str:
    """Render a list of {question, sql, author, score} into the few-shot block.
    Returns empty string when the list is empty so the prompt slot collapses cleanly.
    """
    if not examples:
        return ""
    lines = [EXAMPLES_BLOCK_HEADER.strip(), ""]
    for i, ex in enumerate(examples, 1):
        score = ex.get("score", 0.0)
        author = ex.get("author") or "team"
        lines.append(f"--- Example {i} (similarity {score:.2f}, verified by {author}) ---")
        lines.append(f"Question: {ex['question']}")
        lines.append("SQL:")
        lines.append("```sql")
        lines.append(ex["sql"].strip())
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_hcp_block(facts: list[dict]) -> str:
    if not facts:
        return ""
    lines = [HCP_BLOCK_HEADER.strip(), ""]
    # Group by NPI
    by_npi: dict[str, list[dict]] = {}
    for f in facts:
        by_npi.setdefault(f.get("npi", "?"), []).append(f)
    for npi, items in by_npi.items():
        lines.append(f"NPI {npi}:")
        for f in items:
            cat = f.get("category", "?")
            lines.append(f"  - [{cat}] {f.get('fact_text','').strip()}")
        lines.append("")
    return "\n".join(lines) + "\n"


def build_messages(question: str, schema_summary: str,
                   examples: list[dict] | None = None,
                   hcp_facts: list[dict] | None = None) -> tuple[list[dict], str]:
    """Return (messages, system_prompt) ready for Anthropic SDK.

    Uses str.replace (not .format) so that user-supplied content containing curly
    braces doesn't break templating. The substituted-in blocks themselves are
    trusted (rendered from our own code or from user questions; we never put
    arbitrary tool output through a format call).
    """
    examples_block = render_examples_block(examples or [])
    hcp_context_block = render_hcp_block(hcp_facts or [])
    system = (SYSTEM_PROMPT
              .replace("{schema_summary}", schema_summary)
              .replace("{examples_block}", examples_block)
              .replace("{hcp_context_block}", hcp_context_block))
    user = USER_PROMPT_TEMPLATE.replace("{question}", question)
    msgs = [{"role": "user", "content": user}]
    return msgs, system
