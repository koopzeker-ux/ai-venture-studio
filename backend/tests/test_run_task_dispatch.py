"""Tests for M4.2 dispatch (app.orchestration.run_task.dispatch_task).

subprocess.run is never called in these tests: run_worker_fn,
resolve_worktree_fn, get_changed_files_fn, and run_tests_fn are always
injected fakes, so no real `claude`, `git`, or `pytest` subprocess ever
runs here.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.entities import Task, TaskAttempt, TaskEvent
from app.orchestration.claude_code_adapter import WorkerResult
from app.orchestration.run_task import (
    SuiteRunResult,
    TaskNotFoundError,
    TaskNotReadyError,
    dispatch_task,
)

NOW = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _make_task(db_session, **overrides) -> Task:
    defaults = dict(
        goal="Add a health check endpoint",
        role="builder",
        instructions="Add GET /health returning 200",
        allowed_resources=["backend/app/api/routes.py"],
        forbidden_actions=["do not touch billing code"],
        acceptance_criteria="GET /health returns 200",
        depends_on=[],
        status="READY",
        timeout_seconds=None,
        max_attempts=2,
    )
    defaults.update(overrides)
    task = Task(**defaults)
    db_session.add(task)
    db_session.commit()
    return task


def _ok_worker_result(**overrides) -> WorkerResult:
    defaults = dict(
        ok=True,
        exit_code=0,
        session_id="sess-abc",
        result_text="did the thing",
        usage={"input_tokens": 10, "output_tokens": 20},
        total_cost_usd=0.0123,
        is_error=False,
        error_kind=None,
        error_detail=None,
        stderr_excerpt=None,
    )
    defaults.update(overrides)
    return WorkerResult(**defaults)


def _fail_worker_result(error_kind, error_detail="worker failed") -> WorkerResult:
    return WorkerResult(
        ok=False,
        exit_code=1 if error_kind == "nonzero_exit" else None,
        session_id=None,
        result_text=None,
        usage={},
        total_cost_usd=None,
        is_error=True,
        error_kind=error_kind,
        error_detail=error_detail,
        stderr_excerpt=None,
    )


FAKE_WORKTREE = Path("/fake/worktree/task-1-attempt-1")


def _stub_dispatch(db_session, task, *, worker_result, changed_files=None, test_result=None,
                    resolve_worktree_fn=None, get_changed_files_fn=None, run_tests_fn=None):
    changed_files = [] if changed_files is None else changed_files
    return dispatch_task(
        db_session,
        task.id,
        now=NOW,
        repo_path="/fake/repo",
        run_worker_fn=lambda **kwargs: worker_result,
        resolve_worktree_fn=resolve_worktree_fn or (lambda repo_path, name: FAKE_WORKTREE),
        get_changed_files_fn=get_changed_files_fn or (lambda repo_path, wt_path: changed_files),
        run_tests_fn=run_tests_fn or (lambda wt_path: test_result or SuiteRunResult(passed=True, summary="1 passed")),
    )


# ---------------------------------------------------------------------------
# 11-12: task validation
# ---------------------------------------------------------------------------

def test_ready_task_is_dispatched(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result())
    assert attempt.task_id == task.id


@pytest.mark.parametrize("status", ["PLANNED", "RUNNING", "TESTING", "REVIEW_PENDING", "REVIEWING", "DONE", "FAILED", "BLOCKED"])
def test_non_ready_task_is_explicitly_rejected(db_session, status):
    task = _make_task(db_session, status=status)
    with pytest.raises(TaskNotReadyError) as excinfo:
        _stub_dispatch(db_session, task, worker_result=_ok_worker_result())
    assert str(task.id) in str(excinfo.value)
    assert status in str(excinfo.value)
    # explicit rejection, not a silent no-op: no attempt/event rows written
    assert db_session.scalars(select(TaskAttempt).where(TaskAttempt.task_id == task.id)).all() == []
    assert db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id)).all() == []


def test_dispatching_unknown_task_id_raises(db_session):
    with pytest.raises(TaskNotFoundError):
        dispatch_task(
            db_session, 9999, now=NOW, repo_path="/fake/repo",
            run_worker_fn=lambda **kw: _ok_worker_result(),
            resolve_worktree_fn=lambda rp, n: FAKE_WORKTREE,
            get_changed_files_fn=lambda rp, wp: [],
            run_tests_fn=lambda wp: SuiteRunResult(passed=True, summary="ok"),
        )


# ---------------------------------------------------------------------------
# 13-14: TaskAttempt / TaskEvent creation
# ---------------------------------------------------------------------------

def test_task_attempt_created_correctly(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result())

    assert attempt.task_id == task.id
    assert attempt.attempt_number == 1
    assert attempt.provider == "claude-code"
    assert attempt.worktree_path == f"task-{task.id}-attempt-1"
    assert attempt.branch == f"task-{task.id}-attempt-1"
    assert attempt.started_at == NOW
    assert attempt.finished_at == NOW


def test_task_events_recorded_in_order(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result())

    events = db_session.scalars(
        select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
    ).all()
    transitions = [(e.from_state, e.to_state, e.actor) for e in events]
    assert transitions == [
        ("READY", "RUNNING", "orchestrator"),
        ("RUNNING", "TESTING", "worker"),
        ("TESTING", "REVIEW_PENDING", "orchestrator"),
    ]
    assert all(e.attempt_id == attempt.id for e in events)


# ---------------------------------------------------------------------------
# 15: successful run -> TESTING -> REVIEW_PENDING
# ---------------------------------------------------------------------------

def test_successful_run_reaches_review_pending(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(
        db_session, task, worker_result=_ok_worker_result(),
        changed_files=["backend/app/api/routes.py"],
        test_result=SuiteRunResult(passed=True, summary="12 passed"),
    )

    db_session.refresh(task)
    assert task.status == "REVIEW_PENDING"
    assert attempt.status == "REVIEW_PENDING"
    assert attempt.tests_passed is True
    assert attempt.session_id == "sess-abc"
    assert attempt.findings["cost_usd_estimate"] == 0.0123
    assert attempt.findings["usage"] == {"input_tokens": 10, "output_tokens": 20}


# ---------------------------------------------------------------------------
# 16: pytest failure -> NEEDS_FIX
# ---------------------------------------------------------------------------

def test_independent_pytest_failure_routes_to_needs_fix(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(
        db_session, task, worker_result=_ok_worker_result(),
        changed_files=["backend/app/api/routes.py"],
        test_result=SuiteRunResult(passed=False, summary="3 failed, 9 passed"),
    )

    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"
    assert attempt.status == "NEEDS_FIX"
    assert attempt.tests_passed is False
    assert "3 failed" in attempt.blockers

    events = db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)).all()
    assert (events[-1].from_state, events[-1].to_state, events[-1].actor) == ("TESTING", "NEEDS_FIX", "orchestrator")


# ---------------------------------------------------------------------------
# 17: scope violation -> never success
# ---------------------------------------------------------------------------

def test_scope_violation_never_recorded_as_success(db_session):
    task = _make_task(db_session, allowed_resources=["backend/app/api/routes.py"])
    attempt = _stub_dispatch(
        db_session, task, worker_result=_ok_worker_result(),
        changed_files=["backend/app/api/routes.py", "backend/app/core/config.py"],
    )

    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"
    assert attempt.status != "REVIEW_PENDING"
    assert attempt.status != "DONE"
    assert "config.py" in attempt.findings["scope_violations"][0]
    assert attempt.tests_passed is None  # tests never ran -- scope violated first


def test_scope_violation_even_when_worker_claims_success(db_session):
    """The worker's own self-report is never trusted for scope -- only the independent diff."""
    task = _make_task(db_session, allowed_resources=["backend/app/api/routes.py"])
    worker_result = _ok_worker_result(result_text="Task completed successfully, all changes in scope!")
    attempt = _stub_dispatch(
        db_session, task, worker_result=worker_result,
        changed_files=["backend/app/models/entities.py"],  # actually touched something forbidden
    )
    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"
    assert attempt.status == "NEEDS_FIX"


def test_no_changed_files_is_not_a_scope_violation(db_session):
    task = _make_task(db_session, allowed_resources=["backend/app/api/routes.py"])
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result(), changed_files=[])
    db_session.refresh(task)
    assert task.status == "REVIEW_PENDING"


def test_empty_allowed_resources_fails_closed_on_any_change(db_session):
    task = _make_task(db_session, allowed_resources=[])
    attempt = _stub_dispatch(
        db_session, task, worker_result=_ok_worker_result(),
        changed_files=["backend/app/api/routes.py"],
    )
    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"


# ---------------------------------------------------------------------------
# 18: timeout / crash -> correct M4.1 failure route
# ---------------------------------------------------------------------------

def test_worker_timeout_routes_to_needs_fix_with_system_actor(db_session):
    task = _make_task(db_session, timeout_seconds=60)
    attempt = _stub_dispatch(db_session, task, worker_result=_fail_worker_result("timeout", "worker exceeded timeout of 60s"))

    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"
    assert attempt.status == "NEEDS_FIX"

    events = db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)).all()
    assert (events[-1].from_state, events[-1].to_state, events[-1].actor) == ("RUNNING", "NEEDS_FIX", "system")


def test_worker_crash_nonzero_exit_routes_to_needs_fix_with_worker_actor(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_fail_worker_result("nonzero_exit", "worker reported failure"))

    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"
    events = db_session.scalars(select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)).all()
    assert (events[-1].from_state, events[-1].to_state, events[-1].actor) == ("RUNNING", "NEEDS_FIX", "worker")


def test_timeout_never_produces_terminal_failed_state(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_fail_worker_result("timeout", "timed out"))
    db_session.refresh(task)
    assert task.status not in ("DONE", "FAILED")


def test_orchestrator_own_fault_routes_to_blocked(db_session):
    """resolve_worktree_fn raising is an orchestrator-side fault, not a worker outcome -- BLOCKED, not NEEDS_FIX."""
    task = _make_task(db_session)

    def _boom(repo_path, name):
        raise RuntimeError("worktree lookup exploded")

    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result(), resolve_worktree_fn=_boom)

    db_session.refresh(task)
    assert task.status == "BLOCKED"
    assert attempt.status == "BLOCKED"
    assert "worktree lookup exploded" in attempt.blockers


def test_orchestrator_fault_never_raises_out_of_dispatch(db_session):
    """The whole point of _run_attempt's catch-all: dispatch_task itself must not raise for worker/infra faults."""
    task = _make_task(db_session)

    def _boom(*a, **kw):
        raise ValueError("git diff blew up")

    # Should not raise -- must be converted into a BLOCKED TaskAttempt.
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result(), get_changed_files_fn=_boom)
    assert attempt.status == "BLOCKED"


# ---------------------------------------------------------------------------
# 19: allowed_resources never used as allowedTools
# ---------------------------------------------------------------------------

def test_dispatch_never_passes_allowed_resources_as_allowed_tools(db_session):
    task = _make_task(db_session, allowed_resources=["backend/app/api/routes.py", "backend/app/core/config.py"])
    captured_kwargs = {}

    def _capture_run_worker(**kwargs):
        captured_kwargs.update(kwargs)
        return _ok_worker_result()

    dispatch_task(
        db_session, task.id, now=NOW, repo_path="/fake/repo",
        run_worker_fn=_capture_run_worker,
        resolve_worktree_fn=lambda rp, n: FAKE_WORKTREE,
        get_changed_files_fn=lambda rp, wp: [],
        run_tests_fn=lambda wp: SuiteRunResult(passed=True, summary="ok"),
    )

    assert "allowed_tools" in captured_kwargs
    assert "backend/app/api/routes.py" not in captured_kwargs["allowed_tools"]
    assert "backend/app/core/config.py" not in captured_kwargs["allowed_tools"]
    assert set(captured_kwargs["allowed_tools"]) == {"Read", "Edit", "Write", "Bash(pytest *)"}


def test_prompt_does_not_leak_allowed_resources_paths(db_session):
    """Spec: prompt is built only from goal/instructions/acceptance_criteria/forbidden_actions."""
    task = _make_task(db_session, allowed_resources=["backend/app/very_secret_internal_path.py"])
    captured_kwargs = {}

    def _capture_run_worker(**kwargs):
        captured_kwargs.update(kwargs)
        return _ok_worker_result()

    dispatch_task(
        db_session, task.id, now=NOW, repo_path="/fake/repo",
        run_worker_fn=_capture_run_worker,
        resolve_worktree_fn=lambda rp, n: FAKE_WORKTREE,
        get_changed_files_fn=lambda rp, wp: [],
        run_tests_fn=lambda wp: SuiteRunResult(passed=True, summary="ok"),
    )

    assert "very_secret_internal_path.py" not in captured_kwargs["prompt"]
    assert task.goal in captured_kwargs["prompt"]


# ---------------------------------------------------------------------------
# 20: cost/usage bookkeeping
# ---------------------------------------------------------------------------

def test_cost_usd_estimate_stored_in_findings_cost_eur_stays_zero(db_session):
    task = _make_task(db_session)
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result(total_cost_usd=1.2345))

    assert attempt.findings["cost_usd_estimate"] == 1.2345
    assert attempt.cost_eur == 0.0  # never fabricated from USD -- left at schema default


def test_usage_dict_preserved_in_findings(db_session):
    task = _make_task(db_session)
    usage = {"input_tokens": 500, "output_tokens": 250, "cache_read_input_tokens": 10}
    attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result(usage=usage))
    assert attempt.findings["usage"] == usage


# ---------------------------------------------------------------------------
# Attempt numbering across repeated dispatches
# ---------------------------------------------------------------------------

def test_second_dispatch_after_needs_fix_increments_attempt_number(db_session):
    task = _make_task(db_session)
    _stub_dispatch(db_session, task, worker_result=_fail_worker_result("nonzero_exit"))
    db_session.refresh(task)
    assert task.status == "NEEDS_FIX"

    # A human/orchestrator later moves the task back to READY (out of scope here,
    # M4.2 has no auto-retry) and dispatches again.
    task.status = "READY"
    db_session.add(task)
    db_session.commit()

    second_attempt = _stub_dispatch(db_session, task, worker_result=_ok_worker_result())
    assert second_attempt.attempt_number == 2
