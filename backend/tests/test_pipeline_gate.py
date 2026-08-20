import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.pipeline import passes_candidate_gate, process_raw_signals
from app.db.session import Base
from app.models.entities import Evidence, Opportunity, Signal
from app.services.candidate_filter import STRONG_EVIDENCE_TYPES, EVIDENCE_TYPE_PRODUCT_LAUNCH


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


def _raw(source, source_url, title="", content="", engagement_score=None, is_launch=False):
    return {
        "source": source,
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": None, "is_launch": is_launch},
    }


def test_passes_candidate_gate_rejects_product_launch_alone():
    assert passes_candidate_gate({EVIDENCE_TYPE_PRODUCT_LAUNCH}) is False


def test_passes_candidate_gate_accepts_any_strong_type():
    for evidence_type in STRONG_EVIDENCE_TYPES:
        assert passes_candidate_gate({evidence_type}) is True


def test_launch_only_signal_creates_no_opportunity(db_session):
    # Mirrors the Product Hunt RSS reality: is_launch=True on almost every
    # item, with no pain/purchase/alternative/traction trigger present.
    raw_signals = [
        _raw(
            "fictitious_source_producthunt_like",
            "https://ph.example/launch-only",
            title="New App Launch",
            content="We just launched our new productivity app today",
            is_launch=True,
        )
    ]
    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_new"] == 1
    assert result["candidates_created"] == 0
    assert result["candidates_skipped_cap"] == 0
    assert db_session.scalars(select(Opportunity)).all() == []
    assert db_session.scalars(select(Evidence)).all() == []

    signal = db_session.scalars(select(Signal)).one()
    assert signal.metadata_json["is_launch"] is True


def test_launch_plus_strong_signal_creates_one_opportunity_with_both_evidence(db_session):
    raw_signals = [
        _raw(
            "fictitious_source_producthunt_like",
            "https://ph.example/launch-plus-traction",
            title="New App Launch",
            content="We just launched our new productivity app today",
            engagement_score=500,
            is_launch=True,
        )
    ]
    result = process_raw_signals(db_session, raw_signals, engagement_threshold=50)

    assert result["candidates_created"] == 1

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None

    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    evidence_types = {e.evidence_type for e in evidence}
    assert evidence_types == {"product_launch_signal", "traction_signal"}
    assert len(evidence) == 2


@pytest.mark.parametrize(
    "slug,content,engagement_score",
    [
        ("pain", "wish there was a tool for this", None),
        ("purchase-intent", "would pay for this immediately", None),
        ("alternative-seeking", "looking for an alternative to this", None),
        ("traction", "ordinary content with high traction", 999),
    ],
)
def test_each_strong_type_can_promote_alone(db_session, slug, content, engagement_score):
    raw_signals = [
        _raw(
            "fictitious_source_alpha",
            f"https://alpha.example/{slug}",
            content=content,
            engagement_score=engagement_score,
        )
    ]
    result = process_raw_signals(db_session, raw_signals, engagement_threshold=50)
    assert result["candidates_created"] == 1
    assert db_session.scalars(select(Opportunity)).all()


def test_volume_guardrail_caps_opportunities_and_reports_skipped(db_session):
    max_new = 3
    raw_signals = [
        _raw("fictitious_source_alpha", f"https://alpha.example/strong-{i}", content="wish there was a tool for this")
        for i in range(5)
    ]
    result = process_raw_signals(db_session, raw_signals, max_new_opportunities_per_run=max_new)

    assert result["signals_new"] == 5
    assert result["candidates_created"] == max_new
    assert result["candidates_skipped_cap"] == 5 - max_new

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == max_new

    # All 5 signals are still stored regardless of the cap.
    signals = db_session.scalars(select(Signal)).all()
    assert len(signals) == 5


def test_cap_does_not_suppress_weak_launch_only_signals(db_session):
    # The cap must never mask bad candidate detection: launch-only signals
    # are rejected by the gate, not the cap, and don't count against it.
    raw_signals = [
        _raw("fictitious_source_producthunt_like", f"https://ph.example/{i}", content=f"launch number {i}", is_launch=True)
        for i in range(10)
    ]
    result = process_raw_signals(db_session, raw_signals, max_new_opportunities_per_run=3)

    assert result["candidates_created"] == 0
    assert result["candidates_skipped_cap"] == 0
    assert db_session.scalars(select(Opportunity)).all() == []
