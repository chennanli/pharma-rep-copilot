"""Tests for the LLM provider abstraction.

Each backend is mocked — these tests verify the dispatch logic, env-var
handling, and error messages. No real API/CLI calls are made.
"""
from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

# ---------- helper to reload the module with a fresh LLM_PROVIDER env ----------

def _reload_with_provider(monkeypatch, provider: str):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    # Reload so the module-level PROVIDER constant picks up the new env
    import app.llm_client
    importlib.reload(app.llm_client)
    return app.llm_client


# ---------- provider_info ----------

def test_provider_info_for_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-test-model")
    m = _reload_with_provider(monkeypatch, "anthropic")
    info = m.provider_info()
    assert info["provider"] == "anthropic"
    assert info["model"] == "claude-test-model"
    assert info["needs_key"] is True


def test_provider_info_for_claude_code(monkeypatch):
    m = _reload_with_provider(monkeypatch, "claude_code")
    info = m.provider_info()
    assert info["provider"] == "claude_code"
    assert info["needs_key"] is False


def test_provider_info_for_ollama(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "qwen-test")
    m = _reload_with_provider(monkeypatch, "ollama")
    info = m.provider_info()
    assert info["provider"] == "ollama"
    assert info["model"] == "qwen-test"
    assert info["needs_key"] is False


# ---------- dispatch ----------

def test_chat_with_unknown_provider_raises(monkeypatch):
    m = _reload_with_provider(monkeypatch, "made_up_provider")
    with pytest.raises(RuntimeError) as exc:
        m.chat(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert "made_up_provider" in str(exc.value)


# ---------- claude_code subprocess backend ----------

def test_claude_code_strips_anthropic_env_vars(monkeypatch, tmp_path):
    """The single most important behavior of _chat_claude_code: it must strip
    ANTHROPIC_API_KEY from the subprocess env, otherwise the `claude` CLI
    tries to use the .env placeholder as a real API key and fails."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-be-stripped-too")
    m = _reload_with_provider(monkeypatch, "claude_code")

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "SELECT 1"
    mock_result.stderr = ""

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("subprocess.run", return_value=mock_result) as mock_run:
        m.chat(system="s", messages=[{"role": "user", "content": "q"}])

    # Inspect the env passed to subprocess.run — both vars must be absent
    call_args = mock_run.call_args
    passed_env = call_args.kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in passed_env
    assert "ANTHROPIC_AUTH_TOKEN" not in passed_env


def test_claude_code_missing_cli_raises_helpful_error(monkeypatch):
    m = _reload_with_provider(monkeypatch, "claude_code")
    with patch("shutil.which", return_value=None):
        with pytest.raises(RuntimeError) as exc:
            m.chat(system="s", messages=[{"role": "user", "content": "q"}])
    assert "claude" in str(exc.value).lower()


def test_claude_code_nonzero_exit_raises_with_stderr(monkeypatch):
    m = _reload_with_provider(monkeypatch, "claude_code")

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "Invalid API key"
    mock_result.stderr = ""

    with patch("shutil.which", return_value="/usr/local/bin/claude"), \
         patch("subprocess.run", return_value=mock_result):
        with pytest.raises(RuntimeError) as exc:
            m.chat(system="s", messages=[{"role": "user", "content": "q"}])
    assert "exit=1" in str(exc.value)
    assert "Invalid API key" in str(exc.value)


# ---------- ollama backend ----------

def test_ollama_constructs_correct_message_list(monkeypatch):
    """Ollama wants [{system}, {user}, ...]; verify we build that shape."""
    m = _reload_with_provider(monkeypatch, "ollama")

    mock_client = MagicMock()
    mock_client.chat.return_value = {"message": {"content": "ollama result"}}

    with patch("ollama.Client", return_value=mock_client):
        out = m.chat(
            system="sys-prompt",
            messages=[{"role": "user", "content": "user-question"}],
        )
    assert out == "ollama result"
    args = mock_client.chat.call_args
    msgs_sent = args.kwargs["messages"]
    assert msgs_sent[0] == {"role": "system", "content": "sys-prompt"}
    assert msgs_sent[1] == {"role": "user", "content": "user-question"}


# ---------- anthropic backend (no real API call) ----------

def test_anthropic_extracts_text_block(monkeypatch):
    m = _reload_with_provider(monkeypatch, "anthropic")

    # Mock the Anthropic client's messages.create
    mock_block = MagicMock()
    mock_block.type = "text"
    mock_block.text = "anthropic result"
    mock_response = MagicMock()
    mock_response.content = [mock_block]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response

    with patch("anthropic.Anthropic", return_value=mock_client):
        # Reset module-level _anthropic_client so it picks up our mock
        m._anthropic_client = None
        out = m.chat(
            system="s", messages=[{"role": "user", "content": "q"}]
        )
    assert out == "anthropic result"
