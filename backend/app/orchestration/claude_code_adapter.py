"""Claude Code CLI adapter (M4.2 "One Local Worker").

Invokes the installed `claude` CLI as a subprocess to run exactly one
bounded worker attempt, and turns whatever comes back -- clean JSON,
garbage, a non-zero exit, or a timeout -- into a structured `WorkerResult`
that a caller can persist without ever needing to catch an exception from
this module.

This is the ONLY integration point with Claude Code: no Anthropic SDK, no
direct API calls, no other LLM provider. `subprocess.run` is the sole
mechanism used to invoke it, and this module never calls `claude` (or any
other LLM) for real in its own tests -- `subprocess.run` is always mocked
there (see tests/test_claude_code_adapter.py).

CLI flags used here were verified against `claude --help` (installed CLI
version 2.1.238) before writing this module -- see build_worker_argv().
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Fixed, minimal tool allowlist for the worker. Deliberately NOT derived
# from Task.allowed_resources -- that field holds file PATHS (what the
# worker is allowed to touch), not tool names, and must never be forwarded
# to --allowedTools.
DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Bash(pytest *)")

# Tool names that must never appear in an --allowedTools list: "Bash" alone
# is a blanket grant (any shell command), not the scoped "Bash(pytest *)"
# this adapter is built around.
_FORBIDDEN_TOOL_NAMES = frozenset({"Bash"})

_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*[^\s'\"]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]


def sanitize_text(text: str | None, max_len: int = 2000) -> str | None:
    """Redact secret-shaped substrings and cap length before anything is persisted.

    Applied to every piece of subprocess output (stdout-derived text, stderr,
    exception messages) that might end up in a TaskAttempt/TaskEvent row --
    stderr from a crashed process is exactly the kind of place an accidentally
    logged credential shows up, so it is never stored verbatim.
    """
    if text is None:
        return None
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    if len(redacted) > max_len:
        redacted = redacted[:max_len] + "...[truncated]"
    return redacted


def _decode(value: bytes | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass
class WorkerResult:
    """Structured outcome of one `claude -p ...` worker invocation.

    `total_cost_usd` is Claude Code's own USD estimate, passed through
    as-is -- never converted to EUR here (no exchange-rate contract exists;
    see app.orchestration.run_task).
    """

    ok: bool
    exit_code: int | None
    session_id: str | None
    result_text: str | None
    usage: dict
    total_cost_usd: float | None
    is_error: bool
    error_kind: str | None  # "timeout" | "invalid_json" | "nonzero_exit" | "spawn_error" | None
    error_detail: str | None
    stderr_excerpt: str | None


def _validate_allowed_tools(tools: Sequence[str]) -> None:
    if not tools:
        raise ValueError("allowed_tools must not be empty")
    for tool in tools:
        if tool in _FORBIDDEN_TOOL_NAMES:
            raise ValueError(
                f"blanket tool '{tool}' is not allowed; scope it (e.g. 'Bash(pytest *)')"
            )
        if not tool or tool.startswith("-"):
            raise ValueError(f"invalid tool name {tool!r}")


def build_worker_argv(
    *,
    prompt: str,
    worktree_name: str,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    claude_binary: str = "claude",
) -> list[str]:
    """Build the exact CLI argv for one worker invocation.

    Permission mode is hardcoded to "dontAsk" -- never bypassPermissions,
    never exposed as a caller-controlled parameter -- and the tool list is
    always scoped (see _validate_allowed_tools). No --continue/--resume:
    every call starts a fresh session in a fresh worktree.
    """
    _validate_allowed_tools(allowed_tools)
    if not worktree_name or worktree_name.startswith("-"):
        raise ValueError(f"invalid worktree_name {worktree_name!r}")

    return [
        claude_binary,
        "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--allowedTools", *allowed_tools,
        "--worktree", worktree_name,
        "--bare",
    ]


def run_worker(
    *,
    prompt: str,
    repo_path: str | Path,
    worktree_name: str,
    timeout_seconds: int,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    claude_binary: str = "claude",
) -> WorkerResult:
    """Run exactly one bounded Claude Code worker attempt via subprocess.

    Every failure mode (timeout, unparsable JSON, non-zero exit, failure to
    even launch the process) is converted into a WorkerResult rather than
    raised -- a caller can always persist the result without a try/except.
    """
    argv = build_worker_argv(
        prompt=prompt,
        worktree_name=worktree_name,
        allowed_tools=allowed_tools,
        claude_binary=claude_binary,
    )

    try:
        completed = subprocess.run(
            argv,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return WorkerResult(
            ok=False,
            exit_code=None,
            session_id=None,
            result_text=None,
            usage={},
            total_cost_usd=None,
            is_error=True,
            error_kind="timeout",
            error_detail=f"worker exceeded timeout of {timeout_seconds}s",
            stderr_excerpt=sanitize_text(_decode(exc.stderr)),
        )
    except OSError as exc:
        return WorkerResult(
            ok=False,
            exit_code=None,
            session_id=None,
            result_text=None,
            usage={},
            total_cost_usd=None,
            is_error=True,
            error_kind="spawn_error",
            error_detail=sanitize_text(f"failed to launch worker process: {exc}"),
            stderr_excerpt=None,
        )

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    stderr_excerpt = sanitize_text(stderr) if stderr else None

    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        return WorkerResult(
            ok=False,
            exit_code=completed.returncode,
            session_id=None,
            result_text=None,
            usage={},
            total_cost_usd=None,
            is_error=True,
            error_kind="invalid_json",
            error_detail=sanitize_text(f"could not parse worker JSON output: {exc}"),
            stderr_excerpt=stderr_excerpt,
        )

    if not isinstance(parsed, dict):
        return WorkerResult(
            ok=False,
            exit_code=completed.returncode,
            session_id=None,
            result_text=None,
            usage={},
            total_cost_usd=None,
            is_error=True,
            error_kind="invalid_json",
            error_detail="worker JSON output was not a JSON object",
            stderr_excerpt=stderr_excerpt,
        )

    session_id = parsed.get("session_id")
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    total_cost_usd = parsed.get("total_cost_usd")
    is_error = bool(parsed.get("is_error", completed.returncode != 0))
    result_text = parsed.get("result")
    ok = (completed.returncode == 0) and not is_error

    return WorkerResult(
        ok=ok,
        exit_code=completed.returncode,
        session_id=session_id if isinstance(session_id, str) else None,
        result_text=sanitize_text(result_text) if isinstance(result_text, str) else None,
        usage=usage,
        total_cost_usd=float(total_cost_usd) if isinstance(total_cost_usd, (int, float)) else None,
        is_error=is_error,
        error_kind=None if ok else "nonzero_exit",
        error_detail=None
        if ok
        else sanitize_text(
            str(result_text) if result_text else f"worker reported failure (exit_code={completed.returncode})"
        ),
        stderr_excerpt=stderr_excerpt,
    )
