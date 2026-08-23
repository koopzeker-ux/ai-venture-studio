"""Tests for the Claude Code CLI adapter (M4.2).

subprocess.run is ALWAYS mocked here -- the real `claude` binary is never
invoked. These tests check the CLI argv this adapter builds, and how it
turns subprocess outcomes (clean JSON, malformed JSON, non-zero exit,
timeout) into a structured WorkerResult.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from app.orchestration.claude_code_adapter import (
    DEFAULT_ALLOWED_TOOLS,
    build_worker_argv,
    run_worker,
    sanitize_text,
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# 1-6: CLI argv shape / safety
# ---------------------------------------------------------------------------

def test_build_worker_argv_matches_verified_cli_form():
    argv = build_worker_argv(prompt="do the thing", worktree_name="task-1-attempt-1")
    assert argv == [
        "claude",
        "-p", "do the thing",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--allowedTools", "Read", "Edit", "Write", "Bash(pytest *)",
        "--worktree", "task-1-attempt-1",
        "--safe-mode",
    ]


def test_dontask_always_present():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_minimal_allowed_tools_default():
    assert DEFAULT_ALLOWED_TOOLS == ("Read", "Edit", "Write", "Bash(pytest *)")
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    tools_start = argv.index("--allowedTools") + 1
    tools_end = argv.index("--worktree")
    assert argv[tools_start:tools_end] == list(DEFAULT_ALLOWED_TOOLS)


def test_no_bypass_permissions_flag_ever_appears():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--dangerously-skip-permissions" not in argv
    assert "--allow-dangerously-skip-permissions" not in argv


def test_no_blanket_bash_tool_rejected():
    with pytest.raises(ValueError, match="blanket tool"):
        build_worker_argv(prompt="x", worktree_name="wt", allowed_tools=("Read", "Bash"))


def test_no_continue_or_resume_flags():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    assert "--continue" not in argv
    assert "-c" not in argv
    assert "--resume" not in argv
    assert "-r" not in argv


def test_empty_allowed_tools_rejected():
    with pytest.raises(ValueError):
        build_worker_argv(prompt="x", worktree_name="wt", allowed_tools=())


def test_worktree_name_cannot_look_like_a_flag():
    with pytest.raises(ValueError):
        build_worker_argv(prompt="x", worktree_name="--dangerously-skip-permissions")


# ---------------------------------------------------------------------------
# M4.2 auth-fix (2026-08-23): --bare -> --safe-mode.
#
# The real dogfood run showed --bare blocks headless OAuth auth via
# CLAUDE_CODE_OAUTH_TOKEN (its own help text restricts auth to
# ANTHROPIC_API_KEY/apiKeyHelper, explicitly excluding OAuth). --safe-mode
# (confirmed present on the installed CLI, v2.1.241) keeps auth/built-in
# tools/permissions working normally while still disabling the same class
# of ambient customization --bare did (CLAUDE.md, skills, plugins, hooks,
# MCP, etc). Tests A-F below prove the swap changed exactly what it should
# and nothing else -- no real `claude` invocation, subprocess.run mocked
# throughout.
# ---------------------------------------------------------------------------

def test_A_safe_mode_always_present_in_worker_argv():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    assert "--safe-mode" in argv
    assert argv[-1] == "--safe-mode"


def test_B_bare_flag_never_appears_again():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    assert "--bare" not in argv


def test_C_bypass_permissions_still_impossible_after_auth_fix():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--dangerously-skip-permissions" not in argv
    assert "--allow-dangerously-skip-permissions" not in argv


def test_D_permission_mode_still_exactly_dontask_after_auth_fix():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv.count("--permission-mode") == 1


def test_E_allowed_tools_still_exactly_the_fixed_contract_after_auth_fix():
    argv = build_worker_argv(prompt="x", worktree_name="wt")
    tools_start = argv.index("--allowedTools") + 1
    tools_end = argv.index("--worktree")
    assert argv[tools_start:tools_end] == ["Read", "Edit", "Write", "Bash(pytest *)"]
    assert argv[tools_start:tools_end] == list(DEFAULT_ALLOWED_TOOLS)


def test_F_run_worker_does_not_override_subprocess_environment():
    """No env= kwarg is ever passed to subprocess.run, so the child process
    inherits the orchestrator's own ambient environment -- including
    CLAUDE_CODE_OAUTH_TOKEN -- unchanged. Passing env={} or a stripped-down
    dict here would silently break OAuth auth even with --safe-mode."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0, stdout='{"is_error": false, "result": "ok"}')
        run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    _, kwargs = mock_run.call_args
    assert "env" not in kwargs


# ---------------------------------------------------------------------------
# 7-10: run_worker outcome handling
# ---------------------------------------------------------------------------

def test_successful_json_result_is_parsed():
    payload = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"session_id":"sess-123","total_cost_usd":0.0456,'
        '"usage":{"input_tokens":100,"output_tokens":50},'
        '"result":"did the thing"}'
    )
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout=payload)) as mock_run:
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    mock_run.assert_called_once()
    assert result.ok is True
    assert result.exit_code == 0
    assert result.session_id == "sess-123"
    assert result.total_cost_usd == 0.0456
    assert result.usage == {"input_tokens": 100, "output_tokens": 50}
    assert result.result_text == "did the thing"
    assert result.is_error is False
    assert result.error_kind is None


def test_non_zero_exit_is_structured_failure():
    payload = '{"is_error":true,"result":"crashed halfway","session_id":"s1"}'
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout=payload)):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.ok is False
    assert result.exit_code == 1
    assert result.error_kind == "nonzero_exit"
    assert result.error_detail == "crashed halfway"


def test_timeout_becomes_structured_result_not_exception():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=60)):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.ok is False
    assert result.error_kind == "timeout"
    assert "60s" in result.error_detail
    assert result.exit_code is None


def test_malformed_json_becomes_structured_result_not_exception():
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout="not json at all {{{")):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.ok is False
    assert result.error_kind == "invalid_json"
    assert result.exit_code == 0


def test_json_array_instead_of_object_is_also_invalid():
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout="[1, 2, 3]")):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.ok is False
    assert result.error_kind == "invalid_json"


def test_spawn_failure_becomes_structured_result_not_exception():
    with patch("subprocess.run", side_effect=OSError("claude: command not found")):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.ok is False
    assert result.error_kind == "spawn_error"
    assert result.exit_code is None


# ---------------------------------------------------------------------------
# 21: secret sanitization -- stderr never persisted raw
# ---------------------------------------------------------------------------

def test_fake_secret_in_stderr_is_redacted():
    fake_secret = "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET123456"
    stderr = f"error: ANTHROPIC_API_KEY={fake_secret} rejected by server"
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout="not json", stderr=stderr)):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert result.stderr_excerpt is not None
    assert fake_secret not in result.stderr_excerpt
    assert "[REDACTED]" in result.stderr_excerpt


def test_fake_bearer_token_in_stderr_is_redacted():
    stderr = "request failed: Authorization: Bearer abcDEF1234567890xyzTOKEN"
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout="not json", stderr=stderr)):
        result = run_worker(prompt="x", repo_path="/repo", worktree_name="wt", timeout_seconds=60)

    assert "abcDEF1234567890xyzTOKEN" not in result.stderr_excerpt


def test_sanitize_text_truncates_long_output():
    long_text = "x" * 5000
    sanitized = sanitize_text(long_text, max_len=100)
    assert len(sanitized) <= 130
    assert sanitized.startswith("x" * 100)


def test_sanitize_text_passes_through_none():
    assert sanitize_text(None) is None
