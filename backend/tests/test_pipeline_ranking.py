"""M3.1: deterministic commercial pre-ranking replaces first-come-first-served
candidate capping. Input order must never decide which gate-passing
candidates get promoted to an Opportunity when the volume cap is hit.
"""
import random

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


def _raw(source, source_url, title="", content="", engagement_score=None, is_launch=False):
    return {
        "source": source,
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": None, "is_launch": is_launch},
    }


def _new_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _opportunity_source_urls(session) -> set[str]:
    return {
        e.source_url
        for o in session.scalars(select(Opportunity)).all()
        for e in session.scalars(select(Evidence).where(Evidence.opportunity_id == o.id)).all()
        if e.source_url
    }


def test_shuffled_input_order_yields_identical_top_n(db_session):
    # Five distinct, differently-scored strong candidates. Only the top 2
    # (by pre-rank) should survive a cap of 2, regardless of input order.
    signals = [
        _raw("fictitious_source_a", "https://a.example/purchase-traction", content="would pay for this", engagement_score=200),
        _raw("fictitious_source_b", "https://b.example/pain-only", content="wish there was a tool for this"),
        _raw("fictitious_source_c", "https://c.example/alt-only", content="looking for an alternative to this"),
        _raw("fictitious_source_d", "https://d.example/traction-low", content="ordinary update", engagement_score=60),
        _raw("fictitious_source_e", "https://e.example/traction-mid", content="ordinary update", engagement_score=500),
    ]

    shuffled = list(signals)
    random.Random(42).shuffle(shuffled)

    orderings = [
        signals,
        list(reversed(signals)),
        shuffled,
    ]

    top_n_results = []
    for ordering in orderings:
        session = _new_session()
        result = process_raw_signals(session, ordering, engagement_threshold=50, max_new_opportunities_per_run=2)
        assert result["candidates_created"] == 2
        assert result["candidates_skipped_cap"] == 3
        top_n_results.append(_opportunity_source_urls(session))
        session.close()

    assert top_n_results[0] == top_n_results[1] == top_n_results[2]
    # The highest-scoring candidate (purchase_intent + traction) must always win.
    assert "https://a.example/purchase-traction" in top_n_results[0]


def test_stable_tie_break_uses_source_url_ascending(db_session):
    # Identical evidence composition and engagement -> identical pre-rank
    # score. Only the lexicographically smaller source_url should survive
    # a cap of 1.
    signals = [
        _raw("fictitious_source_a", "https://z-example.test/tied", content="so annoying that this exists"),
        _raw("fictitious_source_b", "https://a-example.test/tied", content="so annoying that this exists too"),
    ]
    result = process_raw_signals(db_session, signals, max_new_opportunities_per_run=1)

    assert result["candidates_created"] == 1
    assert result["candidates_skipped_cap"] == 1

    urls = _opportunity_source_urls(db_session)
    assert urls == {"https://a-example.test/tied"}


def test_modest_purchase_intent_traction_candidate_beats_high_engagement_pure_traction(db_session):
    # Purchase intent + traction (modest engagement=60) vs. a pure-traction
    # candidate with much higher engagement (~6015). With a cap of 1, the
    # combo candidate must win regardless of how high the pure-traction
    # engagement is.
    signals = [
        _raw("fictitious_source_pure_traction", "https://traction.example/high", content="an ordinary popular post", engagement_score=6015),
        _raw("fictitious_source_combo", "https://combo.example/modest", content="would pay for this today", engagement_score=60),
    ]
    result = process_raw_signals(db_session, signals, engagement_threshold=50, max_new_opportunities_per_run=1)

    assert result["candidates_created"] == 1
    urls = _opportunity_source_urls(db_session)
    assert urls == {"https://combo.example/modest"}


def test_gate_still_rejects_launch_only_signals_under_ranking(db_session):
    signals = [
        _raw("fictitious_source_ph_like", "https://ph.example/launch-only", content="we just launched our app", is_launch=True),
    ]
    result = process_raw_signals(db_session, signals)

    assert result["candidates_created"] == 0
    assert result["candidates_skipped_cap"] == 0
    assert db_session.scalars(select(Opportunity)).all() == []


def test_dedupe_still_works_under_ranking(db_session):
    signals = [
        _raw("fictitious_source_a", "https://dup.example/1", content="wish there was a tool for this"),
        _raw("fictitious_source_b", "https://dup.example/1", content="wish there was a tool for this again"),
    ]
    result = process_raw_signals(db_session, signals)

    assert result["signals_new"] == 1
    assert result["signals_duplicate"] == 1
    assert len(db_session.scalars(select(Signal)).all()) == 1


def test_cap_still_bounds_opportunity_count(db_session):
    signals = [
        _raw("fictitious_source_a", f"https://alpha.example/strong-{i}", content="wish there was a tool for this")
        for i in range(7)
    ]
    result = process_raw_signals(db_session, signals, max_new_opportunities_per_run=3)

    assert result["candidates_created"] == 3
    assert result["candidates_skipped_cap"] == 4
    assert len(db_session.scalars(select(Opportunity)).all()) == 3


def test_pre_ranked_opportunities_never_write_score_or_evidence_confidence(db_session):
    signals = [
        _raw("fictitious_source_a", "https://alpha.example/ranked-1", content="would pay for this", engagement_score=200),
        _raw("fictitious_source_b", "https://beta.example/ranked-2", content="wish there was a tool for this"),
    ]
    process_raw_signals(db_session, signals)

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == 2
    for opportunity in opportunities:
        assert opportunity.score is None
        assert opportunity.evidence_confidence is None
