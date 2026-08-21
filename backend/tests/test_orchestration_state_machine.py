from datetime import datetime, timedelta

import pytest

from app.orchestration.state_machine import (
    Actor,
    InvalidTransitionError,
    TaskState,
    TaskStateMachine,
    can_retry,
    dependencies_satisfied,
    is_running_timed_out,
    is_valid_transition,
    resolve_after_needs_fix,
    validate_transition,
)

T0 = datetime(2026, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_happy_path_full_lifecycle_to_done():
    m = TaskStateMachine(max_attempts=2)

    assert m.advance_ready(dependency_states=[], actor=Actor.ORCHESTRATOR) is True
    assert m.state == TaskState.READY

    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    assert m.state == TaskState.RUNNING
    assert m.attempt_number == 1
    assert m.started_at == T0

    m.apply(TaskState.TESTING, Actor.WORKER)
    assert m.state == TaskState.TESTING

    m.apply(TaskState.REVIEW_PENDING, Actor.ORCHESTRATOR)
    assert m.state == TaskState.REVIEW_PENDING

    m.apply(TaskState.REVIEWING, Actor.REVIEWER)
    assert m.state == TaskState.REVIEWING

    m.apply(TaskState.APPROVAL_REQUIRED, Actor.REVIEWER)
    assert m.state == TaskState.APPROVAL_REQUIRED

    m.apply(TaskState.INTEGRATING, Actor.HUMAN)
    assert m.state == TaskState.INTEGRATING

    m.apply(TaskState.DONE, Actor.ORCHESTRATOR)
    assert m.state == TaskState.DONE
    assert m.is_terminal()

    # full transition history recorded in order
    assert [(f.value, t.value) for f, t, _ in m.history] == [
        ("PLANNED", "READY"),
        ("READY", "RUNNING"),
        ("RUNNING", "TESTING"),
        ("TESTING", "REVIEW_PENDING"),
        ("REVIEW_PENDING", "REVIEWING"),
        ("REVIEWING", "APPROVAL_REQUIRED"),
        ("APPROVAL_REQUIRED", "INTEGRATING"),
        ("INTEGRATING", "DONE"),
    ]


def test_happy_path_integrating_can_fail():
    m = TaskStateMachine()
    m.state = TaskState.INTEGRATING
    m.apply(TaskState.FAILED, Actor.ORCHESTRATOR)
    assert m.state == TaskState.FAILED
    assert m.is_terminal()


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "from_state,to_state",
    [
        (TaskState.PLANNED, TaskState.RUNNING),
        (TaskState.PLANNED, TaskState.DONE),
        (TaskState.READY, TaskState.TESTING),
        (TaskState.RUNNING, TaskState.DONE),
        (TaskState.RUNNING, TaskState.REVIEW_PENDING),
        (TaskState.TESTING, TaskState.INTEGRATING),
        (TaskState.REVIEW_PENDING, TaskState.APPROVAL_REQUIRED),
        (TaskState.APPROVAL_REQUIRED, TaskState.DONE),
        (TaskState.DONE, TaskState.RUNNING),
        (TaskState.FAILED, TaskState.RUNNING),
        (TaskState.BLOCKED, TaskState.RUNNING),
    ],
)
def test_illegal_transitions_rejected_for_any_actor(from_state, to_state):
    for actor in Actor:
        assert not is_valid_transition(from_state, to_state, actor)


def test_illegal_transition_raises_with_state_and_actor_context():
    with pytest.raises(InvalidTransitionError) as excinfo:
        validate_transition(TaskState.PLANNED, TaskState.RUNNING, Actor.ORCHESTRATOR)
    err = excinfo.value
    assert err.from_state == TaskState.PLANNED
    assert err.to_state == TaskState.RUNNING
    assert err.actor == Actor.ORCHESTRATOR


def test_terminal_states_have_no_outgoing_transitions():
    for actor in Actor:
        for state in TaskState:
            assert not is_valid_transition(TaskState.DONE, state, actor)
            assert not is_valid_transition(TaskState.FAILED, state, actor)


def test_state_machine_apply_raises_on_illegal_transition():
    m = TaskStateMachine()
    with pytest.raises(InvalidTransitionError):
        m.apply(TaskState.RUNNING, Actor.ORCHESTRATOR)
    # state unchanged after a rejected transition
    assert m.state == TaskState.PLANNED
    assert m.history == []


# ---------------------------------------------------------------------------
# Dependency readiness
# ---------------------------------------------------------------------------

def test_dependencies_satisfied_when_empty():
    assert dependencies_satisfied([]) is True


def test_dependencies_satisfied_when_all_done():
    assert dependencies_satisfied([TaskState.DONE, TaskState.DONE]) is True


def test_dependencies_not_satisfied_when_any_incomplete():
    assert dependencies_satisfied([TaskState.DONE, TaskState.RUNNING]) is False
    assert dependencies_satisfied([TaskState.FAILED]) is False
    assert dependencies_satisfied([TaskState.PLANNED]) is False


def test_advance_ready_blocked_when_dependency_not_done():
    m = TaskStateMachine()
    advanced = m.advance_ready(dependency_states=[TaskState.RUNNING])
    assert advanced is False
    assert m.state == TaskState.PLANNED  # stays PLANNED, not READY
    assert m.history == []


def test_advance_ready_succeeds_when_dependency_done():
    m = TaskStateMachine()
    advanced = m.advance_ready(dependency_states=[TaskState.DONE, TaskState.DONE])
    assert advanced is True
    assert m.state == TaskState.READY


def test_advance_ready_still_enforces_actor_control():
    m = TaskStateMachine()
    with pytest.raises(InvalidTransitionError):
        m.advance_ready(dependency_states=[], actor=Actor.WORKER)
    assert m.state == TaskState.PLANNED


def test_advance_ready_from_wrong_state_raises_regardless_of_dependencies():
    m = TaskStateMachine()
    m.state = TaskState.RUNNING
    with pytest.raises(InvalidTransitionError):
        m.advance_ready(dependency_states=[TaskState.DONE])


# ---------------------------------------------------------------------------
# Retry logic
# ---------------------------------------------------------------------------

def test_can_retry_true_below_max_attempts():
    assert can_retry(attempt_number=1, max_attempts=3) is True


def test_can_retry_false_at_max_attempts():
    assert can_retry(attempt_number=3, max_attempts=3) is False


def test_can_retry_false_beyond_max_attempts():
    assert can_retry(attempt_number=5, max_attempts=3) is False


def test_resolve_after_needs_fix_retries_when_budget_remains():
    assert resolve_after_needs_fix(attempt_number=1, max_attempts=2) == TaskState.RUNNING


def test_resolve_after_needs_fix_blocks_when_exhausted():
    assert resolve_after_needs_fix(attempt_number=2, max_attempts=2) == TaskState.BLOCKED


def test_state_machine_retries_needs_fix_into_running():
    m = TaskStateMachine(max_attempts=2)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1  # one attempt already used, budget for one more

    next_state = m.resolve_needs_fix(actor=Actor.ORCHESTRATOR)
    assert next_state == TaskState.RUNNING
    assert m.state == TaskState.RUNNING


def test_state_machine_retry_then_start_running_bumps_attempt_number():
    m = TaskStateMachine(max_attempts=3)
    m.advance_ready([])
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    assert m.attempt_number == 1

    m.apply(TaskState.NEEDS_FIX, Actor.WORKER)
    next_state = m.resolve_needs_fix()
    assert next_state == TaskState.RUNNING
    assert m.state == TaskState.RUNNING

    # orchestrator actually dispatches the retried attempt
    m.state = TaskState.READY
    m.start_running(now=T0 + timedelta(minutes=5), actor=Actor.ORCHESTRATOR)
    assert m.attempt_number == 2
    assert m.state == TaskState.RUNNING


def test_state_machine_retries_exhausted_goes_to_blocked():
    m = TaskStateMachine(max_attempts=1)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1  # already used the only allowed attempt

    next_state = m.resolve_needs_fix(actor=Actor.ORCHESTRATOR)
    assert next_state == TaskState.BLOCKED
    assert m.state == TaskState.BLOCKED


def test_resolve_needs_fix_respects_actor_control_on_retry():
    m = TaskStateMachine(max_attempts=5)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 0
    with pytest.raises(InvalidTransitionError):
        m.resolve_needs_fix(actor=Actor.WORKER)  # only ORCHESTRATOR may resolve retries


def test_resolve_needs_fix_respects_actor_control_on_block():
    m = TaskStateMachine(max_attempts=1)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1
    # HUMAN is not permitted to drive NEEDS_FIX -> BLOCKED
    with pytest.raises(InvalidTransitionError):
        m.resolve_needs_fix(actor=Actor.HUMAN)


# ---------------------------------------------------------------------------
# Timeout detection
# ---------------------------------------------------------------------------

def test_is_running_timed_out_false_before_deadline():
    assert is_running_timed_out(started_at=T0, timeout_seconds=600, now=T0 + timedelta(seconds=599)) is False


def test_is_running_timed_out_true_at_or_past_deadline():
    assert is_running_timed_out(started_at=T0, timeout_seconds=600, now=T0 + timedelta(seconds=600)) is True
    assert is_running_timed_out(started_at=T0, timeout_seconds=600, now=T0 + timedelta(seconds=900)) is True


def test_is_running_timed_out_false_when_no_timeout_configured():
    assert is_running_timed_out(started_at=T0, timeout_seconds=None, now=T0 + timedelta(days=999)) is False


def test_is_running_timed_out_false_when_not_started():
    assert is_running_timed_out(started_at=None, timeout_seconds=60, now=T0) is False


def test_state_machine_check_timeout_transitions_running_to_needs_fix():
    m = TaskStateMachine(timeout_seconds=300)
    m.advance_ready([])
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)

    timed_out = m.check_timeout(now=T0 + timedelta(seconds=301))
    assert timed_out is True
    assert m.state == TaskState.NEEDS_FIX
    assert m.history[-1] == (TaskState.RUNNING, TaskState.NEEDS_FIX, Actor.SYSTEM)


def test_state_machine_check_timeout_no_op_before_deadline():
    m = TaskStateMachine(timeout_seconds=300)
    m.advance_ready([])
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)

    timed_out = m.check_timeout(now=T0 + timedelta(seconds=10))
    assert timed_out is False
    assert m.state == TaskState.RUNNING


def test_state_machine_check_timeout_no_op_when_not_running():
    m = TaskStateMachine(timeout_seconds=300)
    assert m.check_timeout(now=T0) is False
    assert m.state == TaskState.PLANNED


def test_timeout_then_retry_flows_through_normal_retry_logic():
    m = TaskStateMachine(timeout_seconds=60, max_attempts=2)
    m.advance_ready([])
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    assert m.check_timeout(now=T0 + timedelta(seconds=61)) is True
    assert m.state == TaskState.NEEDS_FIX

    next_state = m.resolve_needs_fix()
    assert next_state == TaskState.RUNNING  # attempt_number(1) < max_attempts(2)


# ---------------------------------------------------------------------------
# Actor / transition rules (beyond the retry-specific cases above)
# ---------------------------------------------------------------------------

def test_only_worker_can_mark_running_as_testing():
    for actor in Actor:
        expected = actor is Actor.WORKER
        assert is_valid_transition(TaskState.RUNNING, TaskState.TESTING, actor) is expected


def test_only_reviewer_can_drive_review_pending_to_reviewing():
    for actor in Actor:
        expected = actor is Actor.REVIEWER
        assert is_valid_transition(TaskState.REVIEW_PENDING, TaskState.REVIEWING, actor) is expected


def test_only_human_can_approve_integration():
    for actor in Actor:
        expected = actor is Actor.HUMAN
        assert is_valid_transition(TaskState.APPROVAL_REQUIRED, TaskState.INTEGRATING, actor) is expected


def test_running_to_needs_fix_allowed_for_worker_and_system_only():
    for actor in Actor:
        expected = actor in (Actor.WORKER, Actor.SYSTEM)
        assert is_valid_transition(TaskState.RUNNING, TaskState.NEEDS_FIX, actor) is expected


def test_worker_cannot_approve_or_integrate():
    assert not is_valid_transition(TaskState.APPROVAL_REQUIRED, TaskState.INTEGRATING, Actor.WORKER)
    assert not is_valid_transition(TaskState.REVIEWING, TaskState.APPROVAL_REQUIRED, Actor.WORKER)


def test_human_cannot_dispatch_ready_to_running():
    assert not is_valid_transition(TaskState.READY, TaskState.RUNNING, Actor.HUMAN)


# ---------------------------------------------------------------------------
# BLOCKED recovery
# ---------------------------------------------------------------------------

def test_blocked_recovers_to_ready_via_human():
    m = TaskStateMachine()
    m.state = TaskState.BLOCKED
    m.apply(TaskState.READY, Actor.HUMAN)
    assert m.state == TaskState.READY


def test_blocked_recovers_to_planned_via_human():
    m = TaskStateMachine()
    m.state = TaskState.BLOCKED
    m.apply(TaskState.PLANNED, Actor.HUMAN)
    assert m.state == TaskState.PLANNED


def test_blocked_cannot_be_recovered_by_non_human_actors():
    for actor in (Actor.ORCHESTRATOR, Actor.WORKER, Actor.REVIEWER, Actor.SYSTEM):
        assert not is_valid_transition(TaskState.BLOCKED, TaskState.READY, actor)
        assert not is_valid_transition(TaskState.BLOCKED, TaskState.PLANNED, actor)


def test_blocked_task_can_run_full_retry_cycle_again_after_recovery():
    m = TaskStateMachine(max_attempts=1)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1
    assert m.resolve_needs_fix() == TaskState.BLOCKED

    # human intervenes (e.g. raises the budget) and resets the task
    m.max_attempts = 2
    m.apply(TaskState.READY, Actor.HUMAN)
    assert m.state == TaskState.READY

    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    assert m.state == TaskState.RUNNING
    assert m.attempt_number == 2


def test_blocked_is_not_terminal():
    m = TaskStateMachine()
    m.state = TaskState.BLOCKED
    assert m.is_terminal() is False
