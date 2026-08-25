"""LEAD (M3.3 pre-review): Alembic migration making experiments.budget_eur
nullable.

Same pattern as test_m3_2_confidence_nullable_migration.py /
test_m3_3_critic_summary_migration.py: runs the real `alembic upgrade head`
path against a throwaway SQLite file. See
app.models.entities.Experiment.budget_eur for the field's rationale.
"""
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.entities import Experiment, Opportunity

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture()
def migrated_db(tmp_path, monkeypatch):
    """See test_alembic_migrations.py's identical fixture for why the root
    logger's handlers are snapshotted/restored around the upgrade call.
    """
    root_logger = logging.getLogger()
    saved_handlers = list(root_logger.handlers)
    saved_level = root_logger.level

    db_path = tmp_path / "m3_3_experiment_budget_nullable_migration_test.sqlite3"
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


def _make_opportunity(db) -> Opportunity:
    opp = Opportunity(slug="budget-migration-opp", title="T", thesis="Thesis")
    db.add(opp)
    db.commit()
    db.refresh(opp)
    return opp


def test_upgrade_head_makes_budget_eur_nullable(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("experiments")}
    assert columns["budget_eur"]["nullable"] is True


def test_upgrade_preserves_other_experiment_columns(migrated_db):
    """Additive migration: no other Experiment column is touched."""
    engine, _ = migrated_db
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("experiments")}
    expected_unchanged = {
        "id": False,
        "opportunity_id": False,
        "hypothesis": False,
        "critical_assumption": False,
        "cheapest_test": False,
        "success_criteria": False,
        "stop_criteria": False,
        "status": False,
        "created_at": False,
    }
    for name, nullable in expected_unchanged.items():
        assert name in columns
        assert columns[name]["nullable"] is nullable, name


def test_new_experiment_can_leave_budget_eur_null(migrated_db):
    engine, _ = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = _make_opportunity(db)
        exp = Experiment(
            opportunity_id=opp.id, hypothesis="h", critical_assumption="c",
            cheapest_test="t", budget_eur=None, success_criteria="s", stop_criteria="k",
        )
        db.add(exp)
        db.commit()
        db.refresh(exp)
        assert exp.budget_eur is None
    finally:
        db.close()


def test_experiment_can_still_hold_a_real_known_budget(migrated_db):
    engine, _ = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = _make_opportunity(db)
        exp = Experiment(
            opportunity_id=opp.id, hypothesis="h", critical_assumption="c",
            cheapest_test="t", budget_eur=150.0, success_criteria="s", stop_criteria="k",
        )
        db.add(exp)
        db.commit()
        db.refresh(exp)
        assert exp.budget_eur == 150.0
    finally:
        db.close()


def test_downgrade_backfills_null_budget_before_restoring_not_null(migrated_db):
    """A row written NULL after the upgrade must not break the downgrade --
    the migration's own downgrade() backfills NULLs to 0.0 first."""
    engine, cfg = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = _make_opportunity(db)
        exp = Experiment(
            opportunity_id=opp.id, hypothesis="h", critical_assumption="c",
            cheapest_test="t", budget_eur=None, success_criteria="s", stop_criteria="k",
        )
        db.add(exp)
        db.commit()
        experiment_id = exp.id
    finally:
        db.close()

    command.downgrade(cfg, "-1")

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("experiments")}
    assert columns["budget_eur"]["nullable"] is False

    db2 = Session()
    try:
        refreshed = db2.get(Experiment, experiment_id)
        assert refreshed.budget_eur == 0.0  # backfilled, not left NULL / not a downgrade failure
    finally:
        db2.close()

    # round-trip: upgrading again must still work
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert {c["name"]: c for c in inspector.get_columns("experiments")}["budget_eur"]["nullable"] is True
