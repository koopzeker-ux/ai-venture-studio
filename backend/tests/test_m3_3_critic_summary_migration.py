"""M3.3 BUILDER: Alembic migration adding Opportunity.critic_summary.

Same pattern as test_m3_2_confidence_nullable_migration.py: runs the real
`alembic upgrade head` path against a throwaway SQLite file. Persistence
slice only -- no Critic agent, no LLM call, no scoring/recommendation
logic. See app.models.entities.Opportunity.critic_summary for the field's
rationale.
"""
import logging
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.entities import Opportunity

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

    db_path = tmp_path / "m3_3_critic_summary_migration_test.sqlite3"
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


def test_upgrade_adds_nullable_critic_summary_column(migrated_db):
    engine, _ = migrated_db
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("opportunities")}
    assert "critic_summary" in columns
    assert columns["critic_summary"]["nullable"] is True


def test_upgrade_preserves_existing_opportunity_columns(migrated_db):
    """Additive migration: no existing Opportunity column is dropped,
    renamed, or has its nullability changed."""
    engine, _ = migrated_db
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("opportunities")}

    expected_unchanged = {
        "id": False,
        "slug": False,
        "title": False,
        "thesis": False,
        "business_model": True,
        "status": False,
        "score": True,
        "evidence_confidence": True,
        "score_breakdown": False,
        "research_summary": True,
        "created_at": False,
        "updated_at": False,
    }
    for name, nullable in expected_unchanged.items():
        assert name in columns
        assert columns[name]["nullable"] is nullable, name


def test_new_opportunity_can_leave_critic_summary_null(migrated_db):
    engine, _ = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = Opportunity(slug="t-null", title="T", thesis="Thesis")
        db.add(opp)
        db.commit()
        db.refresh(opp)
        assert opp.critic_summary is None
    finally:
        db.close()


def test_critic_summary_can_hold_long_text(migrated_db):
    engine, _ = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        long_memo = "Evaluation memo. " * 500  # ~8.5KB, exercises Text (not VARCHAR-limited)
        opp = Opportunity(slug="t-long", title="T", thesis="Thesis", critic_summary=long_memo)
        db.add(opp)
        db.commit()
        db.refresh(opp)
        assert opp.critic_summary == long_memo
    finally:
        db.close()


def test_downgrade_drops_only_critic_summary(migrated_db):
    engine, cfg = migrated_db
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        opp = Opportunity(
            slug="t-downgrade",
            title="T",
            thesis="Thesis",
            critic_summary="memo to be dropped on downgrade",
            research_summary="research summary must survive",
            score=42.0,
            evidence_confidence=70.0,
        )
        db.add(opp)
        db.commit()
        opp_id = opp.id
    finally:
        db.close()

    command.downgrade(cfg, "-1")

    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("opportunities")}
    assert "critic_summary" not in columns
    # Sibling columns untouched by the downgrade.
    assert "research_summary" in columns
    assert "score" in columns
    assert "evidence_confidence" in columns

    # Data for the row survives (raw SQL: the ORM model still declares
    # critic_summary, which no longer exists on the downgraded table).
    with engine.connect() as conn:
        from sqlalchemy import text

        row = conn.execute(
            text("SELECT research_summary, score, evidence_confidence FROM opportunities WHERE id = :id"),
            {"id": opp_id},
        ).one()
        assert row[0] == "research summary must survive"
        assert row[1] == 42.0
        assert row[2] == 70.0

    # round-trip: upgrading again must still work and restore the column
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    columns = {c["name"]: c for c in inspector.get_columns("opportunities")}
    assert "critic_summary" in columns
    assert columns["critic_summary"]["nullable"] is True
