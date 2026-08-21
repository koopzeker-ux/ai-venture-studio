"""M4.1 orchestration state machine.

Pure Python, deterministic, no database, no SQLAlchemy, no I/O. This module
owns the *rules* of task orchestration (legal state transitions, actor
control, dependency-readiness, retry/backoff, timeout detection) so that a
future persistence layer (Task/TaskAttempt/TaskEvent, see
``app.models.entities``) can validate state changes before writing them,
without this module ever importing the database.

Deliberately role-agnostic: nothing here is specific to software-development
tasks. A future business-agent task (research, offer, creative, ...) drives
the same state graph.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Mapping


class TaskState(str, enum.Enum):
    """Lifecycle states of an orchestrated Task."""

    PLANNED = "PLANNED"
    READY = "READY"
    RUNNING = "RUNNING"
    TESTING = "TESTING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVIEWING = "REVIEWING"
    NEEDS_FIX = "NEEDS_FIX"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    INTEGRATING = "INTEGRATING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


TERMINAL_STATES = frozenset({TaskState.DONE, TaskState.FAILED})


class Actor(str, enum.Enum):
    """Who is allowed to drive a given state transition.

    SYSTEM is the automated clock/dependency-graph checker (timeouts,
    dependency-readiness) -- it is not an LLM call and performs no I/O; it
    is the actor value used when the orchestrator loop itself detects a
    condition (elapsed time, dependency graph) rather than a human or agent
    reporting an outcome.
    """

    ORCHESTRATOR = "orchestrator"
    WORKER = "worker"
    REVIEWER = "reviewer"
    HUMAN = "human"
    SYSTEM = "system"


class InvalidTransitionError(Exception):
    """Raised when a state transition is not legal for the given actor."""

    def __init__(self, from_state: TaskState, to_state: TaskState, actor: Actor):
        self.from_state = from_state
        self.to_state = to_state
        self.actor = actor
        super().__init__(
            f"Illegal transition {from_state.value} -> {to_state.value} for actor {actor.value}"
        )


# Transition table: from_state -> to_state -> actors allowed to perform it.
#
# PLANNED -> READY is gated additionally by dependency-readiness (see
# `dependencies_satisfied` / `TaskStateMachine.advance_ready`) on top of the
# actor check below.
#
# NEEDS_FIX -> RUNNING is gated additionally by retry budget (see
# `can_retry` / `TaskStateMachine.resolve_needs_fix`).
#
# LEAD pre-review correction (2026-08-21): every non-terminal state below
# carries a "-> BLOCKED" edge -- the approved M4 plan's generic
# "(elke actieve staat) -> BLOCKED" rule (unrecoverable error, stale
# worktree, or an explicit human PAUSE). The first BUILDER/INTELLIGENCE
# handoff only had this for NEEDS_FIX, and had three edges that disagreed
# with the approved transition spec: REVIEWING had no path straight to
# INTEGRATING (so every reviewed task was forced through human approval,
# even when policy didn't require it), INTEGRATING routed a merge
# conflict/regression failure to the terminal FAILED state instead of the
# recoverable BLOCKED state, and APPROVAL_REQUIRED routed a human rejection
# into NEEDS_FIX (silently auto-retrying work a human had just said no to)
# instead of FAILED. BLOCKED -> PLANNED was also dropped in favor of the
# plan's literal "BLOCKED -> READY of RUNNING" pair. See the corrected
# transitions marked below; see the docstring reasoning in each removed
# edge's place for why it was judged a bug rather than a valid
# interpretation. Actor choices below (e.g. WORKER driving RUNNING ->
# TESTING rather than a generic "systeem" actor) were left as INTELLIGENCE
# designed them -- richer actor attribution for the audit trail, and not a
# functional gap against the plan, so not "corrected".
#
# LEAD REVIEWER-driven correction (2026-08-21, round 2): REVIEWER's
# independent validation raised two further findings against the approved
# plan, both resolved here:
#
# 1. RUNNING -> FAILED (direct, ORCHESTRATOR-only) is REMOVED. The approved
#    plan's own SS4 table names this edge for "crash/timeout", but the same
#    plan's SS11 recovery table and SS9 bounded-fix-loop section both
#    describe a worker crash as retry-eligible ("Worker crash ->
#    heartbeat/timeout-detectie -> NEEDS_FIX, nieuwe attempt"), and the
#    entire point of the bounded fix-loop is to avoid a hard stop on a
#    possibly-transient failure. Read as a whole, the plan's intent is that
#    crash/timeout feeds the *same* retry/fix/block route as any other
#    failure -- RUNNING -> NEEDS_FIX (already correctly wired for timeout
#    detection via `TaskStateMachine.check_timeout()`) -> either RUNNING
#    again (retry budget remains) or BLOCKED (exhausted). A truly
#    unrecoverable RUNNING failure still reaches BLOCKED via the generic
#    active-state rule below, never a silent, unconditional FAILED.
# 2. NEEDS_FIX -> BLOCKED and INTEGRATING -> BLOCKED are widened to include
#    Actor.HUMAN. These two edges have their own plan-specific systeem-only
#    trigger ("attempts op" / "conflict/regressiefout"), which is why the
#    first correction above deliberately left HUMAN out of them -- but
#    NEEDS_FIX and INTEGRATING are still *active states*, and the approved
#    plan's generic rule ("elke actieve staat -> BLOCKED | ... | systeem OF
#    mens") does not carve out an exception for states that also happen to
#    have their own narrower automatic trigger. The systeem-only trigger and
#    the human-pause trigger both target the same (from, to) edge, so the
#    actor set is their union, not either one alone.
TRANSITIONS: Mapping[TaskState, Mapping[TaskState, frozenset[Actor]]] = {
    TaskState.PLANNED: {
        TaskState.READY: frozenset({Actor.ORCHESTRATOR}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.READY: {
        TaskState.RUNNING: frozenset({Actor.ORCHESTRATOR}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.RUNNING: {
        TaskState.TESTING: frozenset({Actor.WORKER}),
        TaskState.NEEDS_FIX: frozenset({Actor.WORKER, Actor.SYSTEM}),
        # REMOVED (round 2): no more direct, unconditioned RUNNING -> FAILED.
        # Crash/timeout now routes through NEEDS_FIX like any other failure
        # -- see the round-2 comment above.
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.TESTING: {
        TaskState.REVIEW_PENDING: frozenset({Actor.ORCHESTRATOR}),
        TaskState.NEEDS_FIX: frozenset({Actor.ORCHESTRATOR}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.REVIEW_PENDING: {
        TaskState.REVIEWING: frozenset({Actor.REVIEWER}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.REVIEWING: {
        TaskState.APPROVAL_REQUIRED: frozenset({Actor.REVIEWER}),
        # CORRECTED: missing from the original handoff. The approved plan
        # (SS4/SS13) makes REVIEWING -> APPROVAL_REQUIRED vs. -> INTEGRATING
        # a *policy* decision (does this task's required_approvals list
        # demand a human?), not something that only ever goes one way --
        # without this edge, no task could ever integrate without a human,
        # regardless of policy.
        TaskState.INTEGRATING: frozenset({Actor.REVIEWER}),
        TaskState.NEEDS_FIX: frozenset({Actor.REVIEWER}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.APPROVAL_REQUIRED: {
        TaskState.INTEGRATING: frozenset({Actor.HUMAN}),
        # CORRECTED: was NEEDS_FIX in the original handoff, which meant a
        # human's rejection silently triggered another automatic worker
        # attempt instead of stopping the task. The approved plan's
        # transition spec is explicit: rejection -> FAILED (terminal).
        TaskState.FAILED: frozenset({Actor.HUMAN}),
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.INTEGRATING: {
        TaskState.DONE: frozenset({Actor.ORCHESTRATOR}),
        # CORRECTED: was FAILED (terminal) in the original handoff. The
        # approved plan (SS11, recovery table) treats a merge conflict or
        # post-merge regression failure as BLOCKED -- human-recoverable,
        # requeueable -- not a dead end. FAILED is no longer reachable from
        # INTEGRATING at all. WIDENED (round 2): HUMAN added alongside the
        # systeem-only conflict/regression trigger -- see the round-2
        # comment above.
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.NEEDS_FIX: {
        TaskState.RUNNING: frozenset({Actor.ORCHESTRATOR}),
        # WIDENED (round 2): HUMAN added alongside the systeem-only
        # attempts-exhausted trigger -- see the round-2 comment above.
        TaskState.BLOCKED: frozenset({Actor.ORCHESTRATOR, Actor.SYSTEM, Actor.HUMAN}),
    },
    TaskState.BLOCKED: {
        TaskState.READY: frozenset({Actor.HUMAN}),
        # CORRECTED: was PLANNED in the original handoff. The approved plan
        # says "BLOCKED -> READY of RUNNING" -- a human either requeues for
        # full re-planning (READY) or resumes execution directly (RUNNING).
        # PLANNED was not part of the approved contract.
        TaskState.RUNNING: frozenset({Actor.HUMAN}),
    },
    TaskState.DONE: {},
    TaskState.FAILED: {},
}


def is_valid_transition(from_state: TaskState, to_state: TaskState, actor: Actor) -> bool:
    """Whether `actor` is allowed to move a task from `from_state` to `to_state`.

    This checks only the actor/transition table -- it does not evaluate the
    extra guards (dependency-readiness, retry budget) that gate
    PLANNED -> READY and NEEDS_FIX -> RUNNING; use `TaskStateMachine` or the
    dedicated guard functions for those.
    """
    allowed_actors = TRANSITIONS.get(from_state, {}).get(to_state)
    return allowed_actors is not None and actor in allowed_actors


def validate_transition(from_state: TaskState, to_state: TaskState, actor: Actor) -> None:
    """Raise InvalidTransitionError if the transition is not legal."""
    if not is_valid_transition(from_state, to_state, actor):
        raise InvalidTransitionError(from_state, to_state, actor)


def dependencies_satisfied(dependency_states: Iterable[TaskState]) -> bool:
    """A task's dependencies are satisfied once every one of them is DONE.

    A task with no dependencies is trivially satisfied.
    """
    return all(state == TaskState.DONE for state in dependency_states)


def can_retry(attempt_number: int, max_attempts: int) -> bool:
    """Whether another attempt is allowed given attempts already made."""
    return attempt_number < max_attempts


def resolve_after_needs_fix(attempt_number: int, max_attempts: int) -> TaskState:
    """Where a NEEDS_FIX task goes next: RUNNING (retry) or BLOCKED (exhausted)."""
    return TaskState.RUNNING if can_retry(attempt_number, max_attempts) else TaskState.BLOCKED


def is_running_timed_out(started_at: datetime | None, timeout_seconds: int | None, now: datetime) -> bool:
    """Whether a RUNNING attempt has exceeded its timeout, as of `now`.

    `now` is always supplied by the caller (never read from a system clock
    here) so this stays deterministic and I/O-free. A task with no timeout
    configured (`timeout_seconds` is None) or that hasn't started yet
    (`started_at` is None) never times out.
    """
    if timeout_seconds is None or started_at is None:
        return False
    return (now - started_at).total_seconds() >= timeout_seconds


@dataclass
class TaskStateMachine:
    """Convenience wrapper composing the guard functions above around one task's state.

    Holds only the fields the guards need (state, attempt bookkeeping,
    timeout bookkeeping) -- no persistence, no task metadata beyond that.
    Every transition still goes through `validate_transition`, so calling
    `apply()` directly with an illegal transition still raises.
    """

    state: TaskState = TaskState.PLANNED
    attempt_number: int = 0
    max_attempts: int = 2
    timeout_seconds: int | None = None
    started_at: datetime | None = None
    history: list[tuple[TaskState, TaskState, Actor]] = field(default_factory=list)

    def apply(self, to_state: TaskState, actor: Actor) -> TaskState:
        """Move to `to_state` if `actor` is allowed to from the current state.

        Plain actor/transition-table check only -- no dependency or retry
        guard. Use `advance_ready` / `resolve_needs_fix` for those.
        """
        validate_transition(self.state, to_state, actor)
        self.history.append((self.state, to_state, actor))
        self.state = to_state
        return self.state

    def advance_ready(self, dependency_states: Iterable[TaskState], actor: Actor = Actor.ORCHESTRATOR) -> bool:
        """Try PLANNED -> READY. Returns True if it happened, False if dependencies aren't ready yet.

        Raises InvalidTransitionError if the actor/transition itself is illegal
        (e.g. called from a state other than PLANNED) regardless of dependency status.
        """
        validate_transition(self.state, TaskState.READY, actor)
        if not dependencies_satisfied(dependency_states):
            return False
        self.history.append((self.state, TaskState.READY, actor))
        self.state = TaskState.READY
        return True

    def start_running(self, now: datetime, actor: Actor = Actor.ORCHESTRATOR) -> TaskState:
        """READY -> RUNNING, recording the attempt start time for timeout tracking."""
        self.apply(TaskState.RUNNING, actor)
        self.attempt_number += 1
        self.started_at = now
        return self.state

    def resolve_needs_fix(self, actor: Actor = Actor.ORCHESTRATOR) -> TaskState:
        """From NEEDS_FIX, move to RUNNING if retries remain, else BLOCKED.

        Does not itself bump attempt_number or started_at when retrying --
        call `start_running` for that once the new attempt actually begins.
        """
        next_state = resolve_after_needs_fix(self.attempt_number, self.max_attempts)
        if next_state == TaskState.BLOCKED:
            actors_allowed = TRANSITIONS[TaskState.NEEDS_FIX][TaskState.BLOCKED]
            if actor not in actors_allowed:
                raise InvalidTransitionError(TaskState.NEEDS_FIX, TaskState.BLOCKED, actor)
        else:
            validate_transition(TaskState.NEEDS_FIX, next_state, actor)
        self.history.append((self.state, next_state, actor))
        self.state = next_state
        return self.state

    def check_timeout(self, now: datetime, actor: Actor = Actor.SYSTEM) -> bool:
        """If RUNNING and the attempt has timed out, transition to NEEDS_FIX.

        Returns True if a timeout transition happened, False otherwise
        (including when the task isn't RUNNING at all).
        """
        if self.state != TaskState.RUNNING:
            return False
        if not is_running_timed_out(self.started_at, self.timeout_seconds, now):
            return False
        self.apply(TaskState.NEEDS_FIX, actor)
        return True

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES
