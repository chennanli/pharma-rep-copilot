"""
LLM provider abstraction so the app can run without an Anthropic API key.

Three supported providers, picked at runtime via the LLM_PROVIDER env var:

  anthropic    — uses ANTHROPIC_API_KEY (pay-per-token). Best quality, fastest.
                 Models: claude-sonnet-4-6, claude-haiku-4-5, claude-opus-4-7.

  claude_code  — shells out to the local `claude` CLI as a subprocess. Uses
                 your existing Claude Code monthly subscription's auth (no
                 per-token API charges, no API key needed).
                 Requires Claude Code installed:
                   https://docs.anthropic.com/en/docs/claude-code
                 Tradeoffs: ~1s extra per call (subprocess overhead);
                 output is parsed from CLI text instead of structured API
                 response; subject to your CC subscription's rate limits.

  ollama       — talks to a local Ollama server. Completely free. No API key,
                 no subscription. Runs entirely on your laptop.
                 Install: https://ollama.com → 'ollama serve'
                          → 'ollama pull qwen2.5-coder:7b'
                 Best small models for NL→SQL: qwen2.5-coder:7b, llama3.1:8b,
                 sqlcoder:7b.

Single function exported: chat(system, messages, max_tokens) → str
"""
from __future__ import annotations

import json
import os

from dotenv import load_dotenv

load_dotenv()

PROVIDER = (os.getenv("LLM_PROVIDER") or "anthropic").lower().strip()


# ---------- public API ----------

def chat(system: str, messages: list[dict], max_tokens: int = 1500) -> str:
    """Send a chat completion and return the response text.

    messages is a list of {"role": "user"|"assistant", "content": "..."} dicts —
    the same shape the Anthropic SDK uses. system is passed as the system prompt.
    """
    if PROVIDER == "anthropic":
        return _chat_anthropic(system, messages, max_tokens)
    if PROVIDER == "claude_code":
        return _chat_claude_code(system, messages, max_tokens)
    if PROVIDER == "ollama":
        return _chat_ollama(system, messages, max_tokens)
    raise RuntimeError(
        f"Unknown LLM_PROVIDER={PROVIDER!r}. "
        f"Set one of: anthropic (needs ANTHROPIC_API_KEY), "
        f"claude_code (uses Claude Code CLI subscription), "
        f"ollama (free local LLM)."
    )


def provider_info() -> dict:
    """Human-readable info for /healthz and the UI footer."""
    if PROVIDER == "anthropic":
        return {
            "provider": "anthropic",
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            "endpoint": "api.anthropic.com",
            "needs_key": True,
        }
    if PROVIDER == "claude_code":
        return {
            "provider": "claude_code",
            "model": "Claude via local `claude` CLI subscription",
            "endpoint": "subprocess: claude --print",
            "needs_key": False,
        }
    if PROVIDER == "ollama":
        return {
            "provider": "ollama",
            "model": os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b"),
            "endpoint": os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            "needs_key": False,
        }
    return {"provider": PROVIDER, "model": "?", "endpoint": "?", "needs_key": True}


# ---------- Anthropic backend ----------

_anthropic_client = None

def _chat_anthropic(system: str, messages: list[dict], max_tokens: int) -> str:
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        _anthropic_client = Anthropic()  # picks up ANTHROPIC_API_KEY
    model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    resp = _anthropic_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    # Anthropic returns a list of content blocks; first text block is what we want.
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            return block.text
    # Fallback if SDK returns differently shaped content
    return getattr(resp.content[0], "text", "")


# ---------- Claude Code subscription backend ----------

def _chat_claude_code(system: str, messages: list[dict], max_tokens: int) -> str:
    """Invoke the local `claude` CLI as a subprocess.

    Uses the auth your Claude Code subscription set up (~/.claude/...) — no API
    key needed. Output is the CLI's stdout, which is just Claude's plain-text
    response (no SDK envelope).

    Tradeoffs vs. the API path:
      * +~1s per call (subprocess + CLI startup)
      * Output parsing is plain text, not structured JSON
      * Counts against your CC subscription's rate limits, not API quota
      * Won't work if the user hasn't installed Claude Code CLI

    Combines the system prompt + message history into one input prompt because
    `claude --print` takes a single string. Order: system context, then each
    message tagged with its role.
    """
    import shutil
    import subprocess

    if shutil.which("claude") is None:
        raise RuntimeError(
            "LLM_PROVIDER=claude_code but `claude` CLI not found on PATH.\n"
            "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code\n"
            "Or switch to LLM_PROVIDER=anthropic (with API key) or "
            "LLM_PROVIDER=ollama (free local LLM)."
        )

    parts = []
    if system:
        parts.append(system)
    for m in messages:
        role = (m.get("role") or "user").lower()
        content = m.get("content") or ""
        # Use simple role tags Claude can follow without needing a fancy template
        parts.append(f"[{role}]\n{content}")
    full_prompt = "\n\n".join(parts)

    # Strip ANTHROPIC_* env vars before invoking claude CLI. Otherwise the CLI
    # picks up our .env's placeholder ANTHROPIC_API_KEY=sk-ant-xxxxx and tries
    # to use it as an external API key (failing with "Invalid API key"), instead
    # of falling back to the user's Claude Code subscription auth at ~/.claude/.
    child_env = os.environ.copy()
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        child_env.pop(var, None)

    try:
        result = subprocess.run(
            ["claude", "--print", full_prompt],
            capture_output=True,
            text=True,
            timeout=180,
            env=child_env,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("claude CLI timed out after 180s") from e
    except FileNotFoundError as e:
        raise RuntimeError("claude CLI disappeared mid-call — check Claude Code install.") from e

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI failed (exit={result.returncode}):\n"
            f"  stderr: {result.stderr.strip()[:500]}\n"
            f"  stdout: {result.stdout.strip()[:500]}"
        )
    return result.stdout.strip()


# ---------- Ollama backend ----------

_ollama_client = None

def _chat_ollama(system: str, messages: list[dict], max_tokens: int) -> str:
    """Talk to a local Ollama server. Falls back to raw HTTP if the ollama
    package isn't installed."""
    global _ollama_client
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

    # Try the official client first
    try:
        if _ollama_client is None:
            import ollama
            _ollama_client = ollama.Client(host=host)
        full_msgs = [{"role": "system", "content": system}] + messages
        resp = _ollama_client.chat(
            model=model,
            messages=full_msgs,
            options={"num_predict": max_tokens, "temperature": 0.2},
        )
        return resp["message"]["content"]
    except ImportError:
        pass  # Fall through to raw HTTP

    # Raw HTTP fallback (no extra dep)
    import urllib.request
    full_msgs = [{"role": "system", "content": system}] + messages
    payload = {
        "model": model,
        "messages": full_msgs,
        "stream": False,
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read().decode("utf-8"))
    return body.get("message", {}).get("content", "")


# ---------- self-test ----------

if __name__ == "__main__":
    info = provider_info()
    print(f"LLM provider: {info['provider']}  model: {info['model']}  endpoint: {info['endpoint']}")
    try:
        out = chat(
            system="You are a SQL helper. Respond with one line of valid Postgres SQL only.",
            messages=[{"role": "user", "content": "Select the current date."}],
            max_tokens=80,
        )
        print(f"[OK] response: {out.strip()[:200]}")
    except Exception as e:
        print(f"[FAIL] {e}")
