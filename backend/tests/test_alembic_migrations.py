"""M4.1 BUILDER: Alembic migrations create the full schema, old and new.

Two migrations exist: a baseline that captures the pre-M4.1 schema (signals,
opportunities, evidence, agent_runs, experiments, approvals, cost_events --
previously only ever created via Base.metadata.create_all(), never
migrated), and a second migration that adds Task/TaskAttempt/TaskEvent plus
a nullable task_id FK on approvals/cost_events.

These tests run the real Alembic migration path (not Base.metadata.create_all)
against a throwaway SQLite file per test, proving `alembic upgrade head` from
an empty database produces the complete, correct schema. alembic/env.py reads
its target URL from app.core.config.settings at run time, so each test
monkeypatches settings.database_url to point at its own temp file -- nothing
here touches the real dev database.
"""
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.config import settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

EXISTING_TABLES = {
    "signals", "opportunities", "evidence", "agent_runs", "experiments",
    "approvals", "cost_events",
}
NEW_TABLES = {"tasks", "task_attempts", "task_events"}


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """Runs `alembic upgrade head` against a fresh, empty SQLite file and
    yields (engine, alembic_config) for assertions. The engine is disposed
    on teardown; the temp file is cleaned up automatically by pytest.

    alembic/env.py calls logging.config.fileConfig() (standard Alembic
    boilerplate, needed for readable `alembic upgrade` CLI output) which
    replaces the ROOT logger's handlers process-wide -- including pytest's
    own caplog handler. Left unguarded, running this fixture once silently
    breaks caplog-based assertions in every test that runs afterward in the
    same pytest process (observed: 10 unrelated failures in
    test_hackernews_resilience.py / test_rss_resilience.py). Snapshot and
    restore the root logger's handlers/level around the whole fixture
    lifetime (not just the upgrade call) so later tests never see it.
    """
    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    db_path = tmp_path / "m4_1_migration_test.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)

    cfg = _alembic_config()
    command.upgrade(cfg, "head")

    engine = create_engine(url)
    try:
        yield engine, cfg
    finally:
        engine.dispose()
        root_logger.handlers = saved_handlers
        root_logger.setLevel(saved_level)


def test_upgrade_head_from_empty_db_creates_full_schema(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    assert EXISTING_TABLES.issubset(tables), "existing M1-M3.1 tables missing from a migrated fresh DB"
    assert NEW_TABLES.issubset(tables), "new M4.1 tables missing from a migrated fresh DB"


def test_task_table_shape(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    columns = {c["name"]: c for c in inspector.get_columns("tasks")}
    expected_nullable = {
        "id": False, "goal": False, "role": False, "instructions": True,
        "allowed_resources": False, "forbidden_actions": False,
        "acceptance_criteria": True, "depends_on": False, "status": False,
        "timeout_seconds": True, "max_attempts": False, "budget_eur": True,
        "required_approvals": False, "provider": True, "model": True,
        "created_at": False, "updated_at": False,
    }
    assert set(columns) == set(expected_nullable)
    for name, nullable in expected_nullable.items():
        assert columns[name]["nullable"] is nullable, f"tasks.{name} nullable mismatch"

    index_names = {ix["name"] for ix in inspector.get_indexes("tasks")}
    assert "ix_tasks_role" in index_names
    assert "ix_tasks_status" in index_names


def test_task_attempt_table_shape_and_fk(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    columns = {c["name"] for c in inspector.get_columns("task_attempts")}
    assert columns == {
        "id", "task_id", "attempt_number", "provider", "model",
        "worktree_path", "branch", "status", "session_id", "summary",
        "files_changed", "tests_passed", "cost_eur", "findings", "blockers",
        "started_at", "finished_at", "created_at",
    }

    fks = inspector.get_foreign_keys("task_attempts")
    assert len(fks) == 1
    assert fks[0]["referred_table"] == "tasks"
    assert fks[0]["constrained_columns"] == ["task_id"]

    index_names = {ix["name"] for ix in inspector.get_indexes("task_attempts")}
    assert "ix_task_attempts_task_id" in index_names


def test_task_event_table_shape_and_fks(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    columns = {c["name"] for c in inspector.get_columns("task_events")}
    assert columns == {
        "id", "task_id", "attempt_id", "from_state", "to_state", "actor",
        "detail", "created_at",
    }

    fks = {fk["constrained_columns"][0]: fk["referred_table"] for fk in inspector.get_foreign_keys("task_events")}
    assert fks == {"task_id": "tasks", "attempt_id": "task_attempts"}


def test_approval_and_cost_event_gain_nullable_task_id_fk(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    for table in ("approvals", "cost_events"):
        columns = {c["name"]: c for c in inspector.get_columns(table)}
        assert "task_id" in columns, f"{table}.task_id missing"
        assert columns["task_id"]["nullable"] is True, f"{table}.task_id must be nullable"

        fks = inspector.get_foreign_keys(table)
        task_fks = [fk for fk in fks if fk["constrained_columns"] == ["task_id"]]
        assert len(task_fks) == 1, f"{table} missing its task_id foreign key"
        assert task_fks[0]["referred_table"] == "tasks"


def test_downgrade_to_base_removes_only_new_schema(migrated_db):
    """Downgrading all the way to base should leave a genuinely empty DB
    (both migrations reverse cleanly) -- proves the downgrade path isn't
    silently broken, even though normal operation only ever goes forward."""
    engine, cfg = migrated_db

    command.downgrade(cfg, "base")
    inspector = inspect(engine)
    remaining = {t for t in inspector.get_table_names() if t != "alembic_version"}
    assert remaining == set(), f"downgrade to base left tables behind: {remaining}"

    # round-trip: upgrading again from the now-empty DB must still work
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert EXISTING_TABLES.issubset(tables)
    assert NEW_TABLES.issubset(tables)
