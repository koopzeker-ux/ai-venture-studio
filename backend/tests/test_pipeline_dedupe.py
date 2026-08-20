import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.pipeline import process_raw_signals
from app.db.session import Base
from app.models.entities import Evidence, Opportunity, Signal


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


def _raw(source, source_url, title="", content="", engagement_score=None, published_at=None):
    return {
        "source": source,
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": published_at},
    }


def test_pipeline_is_source_agnostic_across_two_fictitious_sources(db_session):
    # Two entirely different, made-up source names exercise the exact same
    # code path with no source-specific branching in the pipeline.
    raw_signals = [
        _raw("fictitious_source_alpha", "https://alpha.example/1", content="wish there was a tool for this"),
        _raw("fictitious_source_beta", "https://beta.example/1", content="wish there was a tool for this too"),
    ]
    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_seen"] == 2
    assert result["signals_new"] == 2
    assert result["signals_duplicate"] == 0
    assert result["candidates_created"] == 2

    sources = {s.source for s in db_session.scalars(select(Signal)).all()}
    assert sources == {"fictitious_source_alpha", "fictitious_source_beta"}


def test_duplicate_source_url_is_not_stored_twice(db_session):
    raw_signals = [
        _raw("fictitious_source_alpha", "https://alpha.example/dup", content="normal content"),
        _raw("fictitious_source_beta", "https://alpha.example/dup", content="normal content, different source"),
    ]
    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_seen"] == 2
    assert result["signals_new"] == 1
    assert result["signals_duplicate"] == 1

    stored = db_session.scalars(select(Signal).where(Signal.source_url == "https://alpha.example/dup")).all()
    assert len(stored) == 1


def test_keyword_trigger_creates_opportunity_and_evidence(db_session):
    raw_signals = [
        _raw(
            "fictitious_source_alpha",
            "https://alpha.example/pain",
            title="Frustrated user post",
            content="looking for an alternative to this clunky tool",
        )
    ]
    result = process_raw_signals(db_session, raw_signals)
    assert result["candidates_created"] == 1

    opportunity = db_session.scalars(select(Opportunity)).one()
    assert opportunity.status.value == "discovered"
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None

    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    assert len(evidence) == 1
    assert evidence[0].evidence_type == "pain_point_signal"
    assert evidence[0].confidence == 0.3
    assert evidence[0].independently_confirmed is False
    assert evidence[0].source == "fictitious_source_alpha"
    assert evidence[0].source_url == "https://alpha.example/pain"


def test_engagement_trigger_creates_opportunity_and_evidence(db_session):
    raw_signals = [
        _raw(
            "fictitious_source_beta",
            "https://beta.example/traction",
            title="Popular post",
            content="just a regular update, nothing special in the text",
            engagement_score=250,
        )
    ]
    result = process_raw_signals(db_session, raw_signals, engagement_threshold=50)
    assert result["candidates_created"] == 1

    opportunity = db_session.scalars(select(Opportunity)).one()
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None

    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    assert len(evidence) == 1
    assert evidence[0].evidence_type == "traction_signal"


def test_triggers_work_independently_within_a_batch(db_session):
    raw_signals = [
        _raw("fictitious_source_alpha", "https://alpha.example/only-keyword", content="so annoying that this exists"),
        _raw("fictitious_source_beta", "https://beta.example/only-engagement", content="nothing notable here", engagement_score=999),
    ]
    result = process_raw_signals(db_session, raw_signals, engagement_threshold=50)
    assert result["candidates_created"] == 2

    evidence_types = {e.evidence_type for e in db_session.scalars(select(Evidence)).all()}
    assert evidence_types == {"pain_point_signal", "traction_signal"}


def test_non_matching_signal_creates_no_opportunity(db_session):
    raw_signals = [
        _raw("fictitious_source_alpha", "https://alpha.example/boring", content="an entirely unremarkable update")
    ]
    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_new"] == 1
    assert result["candidates_created"] == 0
    assert db_session.scalars(select(Opportunity)).all() == []
    assert db_session.scalars(select(Evidence)).all() == []
