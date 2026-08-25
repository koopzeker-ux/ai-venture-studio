"""M3.2 BUILDER: Alembic migration for the Evidence/Opportunity dossier columns.

Same pattern as test_alembic_migrations.py (M4.1): runs the real
`alembic upgrade head` path against a throwaway SQLite file, proving the new
revision applies cleanly on top of the existing chain and produces the
expected column/nullability/FK shape. Deliberately a separate module rather
than editing test_alembic_migrations.py, which is M4.1's file.
"""
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.core.config import settings

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

    db_path = tmp_path / "m3_2_migration_test.sqlite3"
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


def test_upgrade_head_adds_evidence_dossier_columns(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    columns = {c["name"]: c for c in inspector.get_columns("evidence")}
    new_nullable_columns = {
        "claim_type": True,
        "stance": True,
        "found_at": True,
        "source_reliability": True,
        "duplicate_of_evidence_id": True,
    }
    for name, nullable in new_nullable_columns.items():
        assert name in columns, f"evidence.{name} missing after migration"
        assert columns[name]["nullable"] is nullable, f"evidence.{name} nullable mismatch"

    # confidence was untouched BY THIS migration (9147d90b16c5) -- but this
    # fixture upgrades all the way to "head", which now also includes a
    # later migration (a943ce8ca51f, LEAD finding-1 decision) that makes
    # confidence nullable. See test_m3_2_confidence_nullable_migration.py
    # for a test that isolates that migration's own before/after effect.
    assert columns["confidence"]["nullable"] is True


def test_evidence_duplicate_of_self_fk_and_index(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    fks = inspector.get_foreign_keys("evidence")
    self_fks = [fk for fk in fks if fk["constrained_columns"] == ["duplicate_of_evidence_id"]]
    assert len(self_fks) == 1, "evidence.duplicate_of_evidence_id missing its self-referential FK"
    assert self_fks[0]["referred_table"] == "evidence"
    assert self_fks[0]["referred_columns"] == ["id"]

    index_names = {ix["name"] for ix in inspector.get_indexes("evidence")}
    assert "ix_evidence_duplicate_of_evidence_id" in index_names


def test_opportunity_gains_nullable_research_summary(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)

    columns = {c["name"]: c for c in inspector.get_columns("opportunities")}
    assert "research_summary" in columns
    assert columns["research_summary"]["nullable"] is True


def test_downgrade_removes_only_m3_2_columns(migrated_db):
    """Isolates this migration's own effect (9147d90b16c5 -> 6b1c524e1012),
    independent of how many later revisions the fixture's "head" now
    includes on top of it (confidence-nullable, M3.3 critic_summary, ...):
    first bring the DB down to 9147d90b16c5 itself -- undoing only later
    migrations -- snapshot columns there, then step back exactly one more
    revision to 6b1c524e1012 and diff. A relative "-N" offset from head, or
    a single downgrade straight from head to 6b1c524e1012, would instead
    conflate this migration's columns with every later migration's columns
    undone along the way."""
    engine, cfg = migrated_db

    command.downgrade(cfg, "9147d90b16c5")
    inspector = inspect(engine)
    evidence_columns_before = {c["name"] for c in inspector.get_columns("evidence")}
    opportunity_columns_before = {c["name"] for c in inspector.get_columns("opportunities")}

    command.downgrade(cfg, "6b1c524e1012")

    inspector = inspect(engine)
    evidence_columns_after = {c["name"] for c in inspector.get_columns("evidence")}
    opportunity_columns_after = {c["name"] for c in inspector.get_columns("opportunities")}

    removed_evidence_columns = evidence_columns_before - evidence_columns_after
    assert removed_evidence_columns == {
        "claim_type", "stance", "found_at", "source_reliability", "duplicate_of_evidence_id",
    }
    removed_opportunity_columns = opportunity_columns_before - opportunity_columns_after
    assert removed_opportunity_columns == {"research_summary"}

    # round-trip: upgrading again must still work
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert "claim_type" in {c["name"] for c in inspector.get_columns("evidence")}
    assert "research_summary" in {c["name"] for c in inspector.get_columns("opportunities")}
