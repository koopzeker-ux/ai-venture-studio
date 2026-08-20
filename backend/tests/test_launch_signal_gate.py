"""Independent REVIEWER coverage for the M2.2 launch-signal promotion gate.

Core architecture decision under test: product_launch_signal is a WEAK
signal. A launch alone ("a product exists") must never promote to an
Opportunity — only a launch combined with at least one STRONG evidence
type (pain_point_signal, purchase_intent_signal, alternative_seeking_signal,
traction_signal) may promote. This mirrors the real Product Hunt RSS feed,
where nearly every item is is_launch=True.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.pipeline import process_raw_signals
from app.db.session import Base
from app.models.entities import Evidence, Opportunity, Signal

BULK_LAUNCH_COUNT = 20
HN_TRACTION_THRESHOLD_DEFAULT = 50


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


def _rss_launch_signal(index):
    """RSS/Product Hunt-shaped: is_launch=True, no strong trigger language,
    no engagement/traction — matches the real PH feed's typical item."""
    return {
        "source": "rss",
        "source_url": f"https://www.producthunt.com/posts/launch-only-{index}",
        "title": f"New Product {index} is live on Product Hunt",
        "content": f"We just launched Product {index}, a brand new productivity app.",
        "metadata": {"engagement_score": None, "published_at": 1735776000.0, "is_launch": True},
    }


def _hn_show_hn_low_traction_signal():
    """HN-shaped: 'Show HN:' title, is_launch=True, engagement below the
    default candidate-detection threshold, no strong trigger language."""
    return {
        "source": "hackernews",
        "source_url": "https://news.ycombinator.com/item?id=987654",
        "title": "Show HN: A brand new productivity app",
        "content": "Show HN: A brand new productivity app I built over the weekend",
        "metadata": {"engagement_score": 5, "published_at": 1735689600.0, "is_launch": True},
    }


def test_bulk_launch_only_batch_creates_zero_candidates(db_session):
    """The single most important negative proof of M2.2: a whole batch of
    launch-only signals (the real-world Product Hunt shape) must never
    promote to Opportunities, no matter the volume."""
    raw_signals = [_rss_launch_signal(i) for i in range(BULK_LAUNCH_COUNT)]

    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_new"] == BULK_LAUNCH_COUNT
    assert result["candidates_created"] == 0
    assert result["candidates_skipped_cap"] == 0

    stored_signals = db_session.scalars(select(Signal)).all()
    assert len(stored_signals) == BULK_LAUNCH_COUNT
    assert all(s.metadata_json["is_launch"] is True for s in stored_signals)

    assert db_session.scalars(select(Opportunity)).all() == []
    assert db_session.scalars(select(Evidence)).all() == []


def test_launch_plus_traction_creates_one_opportunity_with_both_evidence_types(db_session):
    raw_signals = [
        {
            "source": "rss",
            "source_url": "https://www.producthunt.com/posts/launch-with-traction",
            "title": "Popular New Tool launches on Product Hunt",
            "content": "We just launched our new tool today.",
            "metadata": {"engagement_score": 500, "published_at": 1735776000.0, "is_launch": True},
        }
    ]

    result = process_raw_signals(db_session, raw_signals, engagement_threshold=HN_TRACTION_THRESHOLD_DEFAULT)

    assert result["candidates_created"] == 1

    opportunity = db_session.scalars(select(Opportunity)).one()
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None

    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    evidence_types = {e.evidence_type for e in evidence}
    assert evidence_types == {"product_launch_signal", "traction_signal"}
    assert len(evidence) == 2


def test_launch_plus_pain_creates_one_opportunity_with_both_evidence_types(db_session):
    raw_signals = [
        {
            "source": "rss",
            "source_url": "https://www.producthunt.com/posts/launch-with-pain",
            "title": "New Tool launches on Product Hunt",
            "content": "Wish there was a tool for tracking this — so we built one and launched it today.",
            "metadata": {"engagement_score": None, "published_at": 1735776000.0, "is_launch": True},
        }
    ]

    result = process_raw_signals(db_session, raw_signals)

    assert result["candidates_created"] == 1

    opportunity = db_session.scalars(select(Opportunity)).one()
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None

    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    evidence_types = {e.evidence_type for e in evidence}
    assert evidence_types == {"product_launch_signal", "pain_point_signal"}
    assert len(evidence) == 2


def test_show_hn_launch_with_low_traction_creates_no_opportunity(db_session):
    """A 'Show HN:' launch with engagement below the candidate-detection
    threshold and no strong trigger language must not promote — launch +
    weak/insufficient traction is not enough."""
    raw_signals = [_hn_show_hn_low_traction_signal()]

    result = process_raw_signals(db_session, raw_signals, engagement_threshold=HN_TRACTION_THRESHOLD_DEFAULT)

    assert result["signals_new"] == 1
    assert result["candidates_created"] == 0

    signal = db_session.scalars(select(Signal)).one()
    assert signal.metadata_json["is_launch"] is True

    assert db_session.scalars(select(Opportunity)).all() == []
    assert db_session.scalars(select(Evidence)).all() == []
