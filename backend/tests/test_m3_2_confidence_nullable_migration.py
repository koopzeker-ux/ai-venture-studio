"""LEAD (M3.2 REVIEWER finding 1, MEDIUM): Alembic migration making
evidence.confidence nullable.

Same pattern as test_alembic_migrations.py (M4.1) / test_m3_2_evidence_migration.py
(M3.2 BUILDER): runs the real `alembic upgrade head` path against a
throwaway SQLite file. Deliberately a separate module (own migration,
own revision) rather than editing test_m3_2_evidence_migration.py, which
is BUILDER's file for the earlier 9147d90b16c5 revision.
"""
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.entities import Evidence, Opportunity

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

    db_path = tmp_path / "m3_2_confidence_nullable_migration_test.sqlite3"
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


def test_upgrade_head_makes_confidence_nullable(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("evidence")}
    assert columns["confidence"]["nullable"] is True


def test_upgrade_preserves_existing_confidence_values(migrated_db):
    """Additive migration: existing rows keep their real numeric values --
    nothing gets nulled out by upgrading. Written against the real ORM
    models (not raw SQL) so this exercises the exact same insert path the
    app itself uses against a migrated schema."""
    engine, _ = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = Opportunity(slug="t", title="T", thesis="Thesis")
        db.add(opp)
        db.flush()
        row = Evidence(opportunity_id=opp.id, claim="c", evidence_type="market_data", source="s", confidence=0.3)
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.confidence == 0.3
    finally:
        db.close()


def test_downgrade_backfills_null_confidence_before_restoring_not_null(migrated_db):
    """A row written NULL after the upgrade must not break the downgrade --
    the migration's own downgrade() backfills NULLs to 0.5 first."""
    engine, cfg = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = Opportunity(slug="t2", title="T2", thesis="Thesis")
        db.add(opp)
        db.flush()
        row = Evidence(opportunity_id=opp.id, claim="c", evidence_type="research_finding", source="s", confidence=None)
        db.add(row)
        db.commit()
        evidence_id = row.id
    finally:
        db.close()

    command.downgrade(cfg, "-1")

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("evidence")}
    assert columns["confidence"]["nullable"] is False

    db2 = Session()
    try:
        refreshed = db2.get(Evidence, evidence_id)
        assert refreshed.confidence == 0.5  # backfilled, not left NULL / not a downgrade failure
    finally:
        db2.close()

    # round-trip: upgrading again must still work
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert {c["name"]: c for c in inspector.get_columns("evidence")}["confidence"]["nullable"] is True
