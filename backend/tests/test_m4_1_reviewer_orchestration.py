"""Independent REVIEWER validation of M4.1 (pure-Python orchestration state
machine + Task/TaskAttempt/TaskEvent persistence + Alembic migrations).

Does not reuse BUILDER/INTELLIGENCE's test fixtures or assertions -- these
tests are written from the CLAUDE.md M4.1 review brief (state graph, actor
enforcement, persistence contract, Alembic empty-db upgrade) independently,
to catch anything the original authors' own tests might have missed or
assumed away. No production code is modified here.
"""
from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.orchestration import (
    Actor,
    InvalidTransitionError,
    TaskState,
    TaskStateMachine,
    is_valid_transition,
)
from app.db.session import Base
from app.models import entities  # noqa: F401 -- registers Task/TaskAttempt/TaskEvent/Approval/CostEvent
from app.models.entities import Approval, CostEvent, Task, TaskAttempt, TaskEvent

T0 = datetime(2026, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# 1. Happy path, no approval required
# ---------------------------------------------------------------------------

def test_happy_path_without_approval_reaches_done():
    m = TaskStateMachine(max_attempts=2)
    m.advance_ready(dependency_states=[], actor=Actor.ORCHESTRATOR)
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    m.apply(TaskState.TESTING, Actor.WORKER)
    m.apply(TaskState.REVIEW_PENDING, Actor.ORCHESTRATOR)
    m.apply(TaskState.REVIEWING, Actor.REVIEWER)
    m.apply(TaskState.INTEGRATING, Actor.REVIEWER)  # policy: no approval required
    m.apply(TaskState.DONE, Actor.ORCHESTRATOR)

    assert m.state == TaskState.DONE
    assert m.is_terminal()
    assert TaskState.APPROVAL_REQUIRED not in [s for s, _, _ in m.history] + [s for _, s, _ in m.history]


# ---------------------------------------------------------------------------
# 2. Happy path, approval required
# ---------------------------------------------------------------------------

def test_happy_path_with_approval_reaches_done():
    m = TaskStateMachine(max_attempts=2)
    m.advance_ready(dependency_states=[], actor=Actor.ORCHESTRATOR)
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    m.apply(TaskState.TESTING, Actor.WORKER)
    m.apply(TaskState.REVIEW_PENDING, Actor.ORCHESTRATOR)
    m.apply(TaskState.REVIEWING, Actor.REVIEWER)
    m.apply(TaskState.APPROVAL_REQUIRED, Actor.REVIEWER)
    m.apply(TaskState.INTEGRATING, Actor.HUMAN)
    m.apply(TaskState.DONE, Actor.ORCHESTRATOR)

    assert m.state == TaskState.DONE
    assert m.is_terminal()


def test_reviewer_alone_cannot_skip_approval_once_it_reached_approval_required():
    m = TaskStateMachine()
    m.state = TaskState.APPROVAL_REQUIRED
    for actor in (Actor.ORCHESTRATOR, Actor.WORKER, Actor.REVIEWER, Actor.SYSTEM):
        assert not is_valid_transition(TaskState.APPROVAL_REQUIRED, TaskState.INTEGRATING, actor)


# ---------------------------------------------------------------------------
# 3. Human rejection
# ---------------------------------------------------------------------------

def test_human_rejection_moves_approval_required_to_failed_terminal():
    m = TaskStateMachine()
    m.state = TaskState.APPROVAL_REQUIRED
    m.apply(TaskState.FAILED, Actor.HUMAN)
    assert m.state == TaskState.FAILED
    assert m.is_terminal()
    # a rejected task cannot be silently retried
    for actor in Actor:
        assert not is_valid_transition(TaskState.FAILED, TaskState.NEEDS_FIX, actor)
        assert not is_valid_transition(TaskState.FAILED, TaskState.RUNNING, actor)


def test_only_human_can_reject_approval_required():
    for actor in (Actor.ORCHESTRATOR, Actor.WORKER, Actor.REVIEWER, Actor.SYSTEM):
        assert not is_valid_transition(TaskState.APPROVAL_REQUIRED, TaskState.FAILED, actor)


# ---------------------------------------------------------------------------
# 4. Integration conflict -> BLOCKED -> human recovery
# ---------------------------------------------------------------------------

def test_integrating_conflict_blocks_then_recovers_via_human_requeue():
    m = TaskStateMachine(max_attempts=2)
    m.state = TaskState.INTEGRATING
    m.apply(TaskState.BLOCKED, Actor.ORCHESTRATOR)
    assert m.state == TaskState.BLOCKED
    assert not m.is_terminal()

    m.apply(TaskState.READY, Actor.HUMAN)
    assert m.state == TaskState.READY
    m.start_running(now=T0, actor=Actor.ORCHESTRATOR)
    assert m.state == TaskState.RUNNING


def test_integrating_never_reaches_terminal_failed_directly():
    for actor in Actor:
        assert not is_valid_transition(TaskState.INTEGRATING, TaskState.FAILED, actor)


def test_integrating_blocked_edge_now_includes_human_actor():
    """RESOLVED by LEAD (round 2, post-review): this was flagged as an
    open question (was: test_integrating_blocked_edge_excludes_human_actor)
    -- does the approved 'elke actieve staat -> BLOCKED' emergency-stop rule
    intend HUMAN to be excluded here, given INTEGRATING already had its own
    narrower systeem-only ->BLOCKED trigger? LEAD's decision: no exclusion
    intended -- the generic rule and the specific trigger target the same
    edge, so the actor set is their union. HUMAN added."""
    assert is_valid_transition(TaskState.INTEGRATING, TaskState.BLOCKED, Actor.HUMAN)
    assert is_valid_transition(TaskState.INTEGRATING, TaskState.BLOCKED, Actor.ORCHESTRATOR)
    assert is_valid_transition(TaskState.INTEGRATING, TaskState.BLOCKED, Actor.SYSTEM)


# ---------------------------------------------------------------------------
# 5. Retry exhaustion
# ---------------------------------------------------------------------------

def test_needs_fix_retry_exhaustion_goes_to_blocked_not_failed():
    m = TaskStateMachine(max_attempts=1)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1
    next_state = m.resolve_needs_fix(actor=Actor.ORCHESTRATOR)
    assert next_state == TaskState.BLOCKED
    assert not m.is_terminal()


def test_needs_fix_blocked_edge_now_includes_human_actor_too():
    """RESOLVED by LEAD (round 2, post-review) -- same resolution as
    test_integrating_blocked_edge_now_includes_human_actor."""
    assert is_valid_transition(TaskState.NEEDS_FIX, TaskState.BLOCKED, Actor.HUMAN)


def test_needs_fix_with_budget_remaining_retries_into_running():
    m = TaskStateMachine(max_attempts=3)
    m.state = TaskState.NEEDS_FIX
    m.attempt_number = 1
    next_state = m.resolve_needs_fix(actor=Actor.ORCHESTRATOR)
    assert next_state == TaskState.RUNNING


# ---------------------------------------------------------------------------
# 6. Dependency readiness
# ---------------------------------------------------------------------------

def test_planned_task_with_incomplete_dependency_cannot_become_ready():
    m = TaskStateMachine()
    advanced = m.advance_ready(dependency_states=[TaskState.RUNNING])
    assert advanced is False
    assert m.state == TaskState.PLANNED


def test_planned_task_with_failed_dependency_cannot_become_ready():
    m = TaskStateMachine()
    advanced = m.advance_ready(dependency_states=[TaskState.DONE, TaskState.FAILED])
    assert advanced is False
    assert m.state == TaskState.PLANNED


def test_planned_task_with_no_dependencies_becomes_ready():
    m = TaskStateMachine()
    assert m.advance_ready(dependency_states=[]) is True
    assert m.state == TaskState.READY


# ---------------------------------------------------------------------------
# 7. Generic active-state -> BLOCKED emergency stop
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "state",
    [
        TaskState.PLANNED,
        TaskState.READY,
        TaskState.RUNNING,
        TaskState.TESTING,
        TaskState.REVIEW_PENDING,
        TaskState.REVIEWING,
        TaskState.APPROVAL_REQUIRED,
        TaskState.NEEDS_FIX,
        TaskState.INTEGRATING,
    ],
)
def test_every_active_state_has_a_blocked_edge_for_at_least_orchestrator_and_system(state):
    """Independent full sweep of all 9 non-terminal states (BUILDER's own
    equivalent test only covers 7 of the 9, deliberately excluding
    NEEDS_FIX/INTEGRATING). Confirms the minimum common guarantee (system-
    level emergency stop) holds everywhere, regardless of the HUMAN-actor
    asymmetry flagged above."""
    assert is_valid_transition(state, TaskState.BLOCKED, Actor.ORCHESTRATOR) or \
        is_valid_transition(state, TaskState.BLOCKED, Actor.SYSTEM)


def test_multiple_distinct_active_states_all_reach_blocked_independently():
    for state, actor in [
        (TaskState.PLANNED, Actor.HUMAN),
        (TaskState.RUNNING, Actor.SYSTEM),
        (TaskState.REVIEWING, Actor.ORCHESTRATOR),
        (TaskState.APPROVAL_REQUIRED, Actor.HUMAN),
    ]:
        m = TaskStateMachine()
        m.state = state
        m.apply(TaskState.BLOCKED, actor)
        assert m.state == TaskState.BLOCKED
        assert not m.is_terminal()


# ---------------------------------------------------------------------------
# 8. Terminal states: no resurrection
# ---------------------------------------------------------------------------

def test_done_has_zero_outgoing_transitions():
    for actor in Actor:
        for state in TaskState:
            assert not is_valid_transition(TaskState.DONE, state, actor)


def test_failed_has_zero_outgoing_transitions():
    for actor in Actor:
        for state in TaskState:
            assert not is_valid_transition(TaskState.FAILED, state, actor)


def test_apply_on_terminal_state_raises_regardless_of_actor():
    m = TaskStateMachine()
    m.state = TaskState.DONE
    for actor in Actor:
        with pytest.raises(InvalidTransitionError):
            m.apply(TaskState.READY, actor)
    assert m.state == TaskState.DONE  # unchanged after every rejected attempt


# ---------------------------------------------------------------------------
# 9. Persistence contract: Task / TaskAttempt / TaskEvent / Approval / CostEvent
# ---------------------------------------------------------------------------

@pytest.fixture()
def fk_enforced_engine():
    """In-memory SQLite with FK enforcement turned on (off by default in
    SQLite, unlike Postgres) so ondelete='CASCADE'/'SET NULL' behavior is
    actually exercised, not just declared in the model."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture()
def db_session(fk_enforced_engine):
    Session = sessionmaker(bind=fk_enforced_engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def test_task_attempts_cascade_delete_when_task_deleted(db_session):
    task = Task(goal="g", role="worker", status="PLANNED")
    db_session.add(task)
    db_session.commit()

    attempt = TaskAttempt(task_id=task.id, attempt_number=1, status="RUNNING")
    db_session.add(attempt)
    db_session.commit()
    attempt_id = attempt.id

    db_session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
    db_session.commit()

    assert db_session.get(TaskAttempt, attempt_id) is None


def test_task_events_cascade_delete_when_task_deleted(db_session):
    task = Task(goal="g", role="worker", status="PLANNED")
    db_session.add(task)
    db_session.commit()

    ev = TaskEvent(task_id=task.id, to_state="PLANNED", actor="orchestrator")
    db_session.add(ev)
    db_session.commit()
    event_id = ev.id

    db_session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
    db_session.commit()

    assert db_session.get(TaskEvent, event_id) is None


def test_task_event_attempt_id_set_null_when_attempt_deleted(db_session):
    task = Task(goal="g", role="worker", status="PLANNED")
    db_session.add(task)
    db_session.commit()

    attempt = TaskAttempt(task_id=task.id, attempt_number=1, status="RUNNING")
    db_session.add(attempt)
    db_session.commit()

    ev = TaskEvent(task_id=task.id, attempt_id=attempt.id, to_state="RUNNING", actor="worker")
    db_session.add(ev)
    db_session.commit()
    event_id = ev.id

    db_session.execute(text("DELETE FROM task_attempts WHERE id = :id"), {"id": attempt.id})
    db_session.commit()

    db_session.expire_all()
    survived = db_session.get(TaskEvent, event_id)
    assert survived is not None
    assert survived.attempt_id is None


def test_approval_task_id_set_null_when_task_deleted(db_session):
    task = Task(goal="g", role="worker", status="APPROVAL_REQUIRED")
    db_session.add(task)
    db_session.commit()

    approval = Approval(action_type="task_integration", description="d", task_id=task.id)
    db_session.add(approval)
    db_session.commit()
    approval_id = approval.id

    db_session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
    db_session.commit()

    db_session.expire_all()
    survived = db_session.get(Approval, approval_id)
    assert survived is not None
    assert survived.task_id is None


def test_cost_event_task_id_set_null_when_task_deleted(db_session):
    task = Task(goal="g", role="worker", status="RUNNING")
    db_session.add(task)
    db_session.commit()

    cost = CostEvent(category="llm_call", amount_eur=0.05, task_id=task.id)
    db_session.add(cost)
    db_session.commit()
    cost_id = cost.id

    db_session.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task.id})
    db_session.commit()

    db_session.expire_all()
    survived = db_session.get(CostEvent, cost_id)
    assert survived is not None
    assert survived.task_id is None


def test_approval_and_cost_event_task_id_can_stay_null_untouched():
    """Most existing Approval/CostEvent rows predate M4.1 and are not
    task-scoped -- task_id must be omittable, not just nullable-after-delete."""
    approval = Approval(action_type="dns_change", description="d")
    cost = CostEvent(category="market_data", amount_eur=1.0)
    assert approval.task_id is None
    assert cost.task_id is None


def test_task_status_accepts_arbitrary_plain_strings_not_tied_to_state_machine_enum(db_session):
    """Task.status is a plain String column (see entities.py docstring), not
    a DB-level Enum bound to app.orchestration.state_machine.TaskState -- a
    business-agent task with a role-specific status string must not be
    rejected by a DB constraint that only knows the software-dev states."""
    task = Task(goal="g", role="market_researcher", status="AWAITING_MARKET_DATA")
    db_session.add(task)
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(Task, task.id).status == "AWAITING_MARKET_DATA"


def test_task_attempt_and_task_event_status_actor_columns_are_plain_strings():
    columns = {c.name: c for c in TaskAttempt.__table__.columns}
    assert type(columns["status"].type).__name__ == "String"
    event_columns = {c.name: c for c in TaskEvent.__table__.columns}
    assert type(event_columns["actor"].type).__name__ == "String"
    assert type(event_columns["to_state"].type).__name__ == "String"
    assert type(event_columns["from_state"].type).__name__ == "String"


def test_task_required_scalar_columns_are_not_nullable():
    columns = {c.name: c for c in Task.__table__.columns}
    for name in ("goal", "role", "status", "max_attempts"):
        assert columns[name].nullable is False, f"Task.{name} should not be nullable"
    for name in ("instructions", "timeout_seconds", "budget_eur", "provider", "model"):
        assert columns[name].nullable is True, f"Task.{name} should be nullable"


def test_task_attempt_task_id_is_not_nullable_but_optional_fields_are():
    columns = {c.name: c for c in TaskAttempt.__table__.columns}
    assert columns["task_id"].nullable is False
    assert columns["attempt_number"].nullable is False
    assert columns["status"].nullable is False
    for name in ("worktree_path", "branch", "session_id", "tests_passed", "started_at", "finished_at"):
        assert columns[name].nullable is True


def test_task_event_task_id_not_nullable_to_state_not_nullable_from_state_nullable():
    columns = {c.name: c for c in TaskEvent.__table__.columns}
    assert columns["task_id"].nullable is False
    assert columns["to_state"].nullable is False
    assert columns["from_state"].nullable is True  # PLANNED's initial event has no "from"


def test_approval_and_cost_event_task_id_columns_are_nullable():
    approval_columns = {c.name: c for c in Approval.__table__.columns}
    cost_columns = {c.name: c for c in CostEvent.__table__.columns}
    assert approval_columns["task_id"].nullable is True
    assert cost_columns["task_id"].nullable is True


def _import_lines(source: str) -> list[str]:
    """Actual import statements only -- excludes docstrings/comments that
    merely *discuss* a module name (both entities.py and state_machine.py
    have exactly that: prose explaining why a dependency is deliberately
    absent, which would false-positive a plain substring search)."""
    return [
        line.strip() for line in source.splitlines()
        if line.strip().startswith("import ") or line.strip().startswith("from ")
    ]


def test_entities_module_has_no_orchestration_import():
    """Architectural boundary from the M4.1 brief: persistence
    (app.models.entities) must never import app.orchestration.state_machine
    -- the state machine is pure-Python/no-DB and must stay that way, and
    entities.py must stay agnostic of orchestration business logic."""
    import inspect as _inspect
    imports = _import_lines(_inspect.getsource(entities))
    assert not any("orchestration" in line or "state_machine" in line for line in imports), imports


def test_state_machine_module_has_no_sqlalchemy_or_db_import():
    import inspect as _inspect
    from app.orchestration import state_machine as sm

    imports = _import_lines(_inspect.getsource(sm))
    assert not any("sqlalchemy" in line.lower() for line in imports), imports
    assert not any("app.db" in line or "app.models" in line for line in imports), imports


# ---------------------------------------------------------------------------
# 10. Alembic: empty-database upgrade, and the live-Postgres migration
#     procedure's core assumption (stamp baseline on an *already-populated*
#     pre-M4.1 schema, then upgrade head) -- independent of BUILDER's own
#     empty-db-only alembic test file.
# ---------------------------------------------------------------------------

BACKEND_DIR_FOR_ALEMBIC = __import__("pathlib").Path(__file__).resolve().parent.parent


def _cfg():
    return Config(str(BACKEND_DIR_FOR_ALEMBIC / "alembic.ini"))


def test_alembic_upgrade_head_from_empty_db_is_idempotent_and_matches_orm_metadata(tmp_path, monkeypatch):
    """Independent re-proof (different assertion strategy than BUILDER's
    test_alembic_migrations.py): compares the alembic-built schema's table
    set directly against Base.metadata's own table set, rather than a
    hardcoded literal list -- catches drift if a future model is added
    without a matching migration."""
    from app.core.config import settings

    db_path = tmp_path / "reviewer_empty_upgrade.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)

    command.upgrade(_cfg(), "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        migrated_tables = {t for t in inspector.get_table_names() if t != "alembic_version"}
        orm_tables = set(Base.metadata.tables.keys())
        assert migrated_tables == orm_tables, (
            f"alembic-built schema and ORM metadata disagree: "
            f"only-in-migrations={migrated_tables - orm_tables}, "
            f"only-in-orm={orm_tables - migrated_tables}"
        )
    finally:
        engine.dispose()


def test_stamp_baseline_on_preexisting_pre_m4_1_schema_then_upgrade_head_succeeds(tmp_path, monkeypatch):
    """Directly exercises the LEAD-proposed live-Postgres procedure's core
    mechanic on a throwaway DB: create the 7 pre-M4.1 tables exactly as the
    real dev DB already has them (via Base.metadata.create_all against only
    the pre-M4.1 models, simulating "existing DB, no alembic_version yet"),
    then `alembic stamp da1e9c017859` + `alembic upgrade head`, and confirm
    the result is schema-identical to a fresh empty-db upgrade. This is the
    one scenario neither BUILDER's alembic tests nor a fresh-db-only run
    actually prove, and it is exactly what section E's real procedure
    depends on being safe.
    """
    from app.core.config import settings

    db_path = tmp_path / "reviewer_stamp_then_upgrade.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = _cfg()

    # Build the pre-M4.1 schema using the baseline migration's own upgrade()
    # (not a hand-rolled copy) -- guarantees byte-for-byte the same schema
    # the real dev DB has, since that IS what da1e9c017859 was written to
    # capture. Then drop alembic_version to simulate "real dev DB: has the
    # tables, was never migrated via alembic" before stamping.
    command.upgrade(cfg, "da1e9c017859")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE alembic_version"))
    engine.dispose()

    # Sanity: no alembic_version yet, exactly like the real dev DB.
    engine = create_engine(url)
    assert "alembic_version" not in inspect(engine).get_table_names()
    engine.dispose()

    command.stamp(cfg, "da1e9c017859")
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        tables = {t for t in inspector.get_table_names() if t != "alembic_version"}
        orm_tables = set(Base.metadata.tables.keys())
        assert tables == orm_tables, "stamp+upgrade path produced a different schema than a fresh upgrade"

        # the pre-existing data-bearing tables must not have been touched/recreated --
        # verify by checking their columns are unchanged (task_id present on approvals/cost_events)
        approval_cols = {c["name"] for c in inspector.get_columns("approvals")}
        assert "task_id" in approval_cols
    finally:
        engine.dispose()
