"""Independent REVIEWER validation of M3.1's deterministic pre-ranking.

M3.1 replaced first-come-first-served candidate capping with a ranked
pool: all gate-passing candidates in a batch are scored, sorted, and
only then capped. This file independently re-proves, with its own
fixtures (not reused from INTELLIGENCE's test_pipeline_ranking.py),
that:
  - a commercially stronger candidate (purchase_intent + traction, low
    engagement) beats pure high-engagement traction candidates for a
    scarce cap slot, even when added LAST in the input;
  - the lowest pre-ranked pure-traction candidates are the ones that
    fall out of the cap;
  - input order never changes which candidates end up promoted;
  - Opportunity.score and Opportunity.evidence_confidence stay NULL
    for every ranked/promoted Opportunity (pre-rank is not scoring).
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.pipeline import process_raw_signals
from app.db.session import Base
from app.models.entities import Evidence, Opportunity, Signal

CAP = 5

# Six pure-traction-only candidates (single evidence_type: traction_signal),
# with distinct engagement scores including the ~6015 example from the M3.1
# brief. No pain/purchase/alternative trigger language anywhere.
PURE_TRACTION_ENGAGEMENT_SCORES = [80, 200, 500, 1000, 3000, 6015]

# The commercially interesting candidate: purchase_intent + traction, but
# with much lower engagement (60) than most of the pure-traction pool.
COMBO_URL = "https://reviewer.example/combo-purchase-traction-60"


def _pure_traction_url(engagement_score: int) -> str:
    return f"https://reviewer.example/pure-traction-{engagement_score}"


def _pure_traction_signal(engagement_score: int) -> dict:
    return {
        "source": "rss",
        "source_url": _pure_traction_url(engagement_score),
        "title": "Ordinary popular post",
        "content": "an ordinary post with nothing but raw popularity",
        "metadata": {"engagement_score": engagement_score, "published_at": None, "is_launch": False},
    }


def _combo_signal() -> dict:
    return {
        "source": "hackernews",
        "source_url": COMBO_URL,
        "title": "A modestly popular but commercially promising post",
        "content": "would pay for this today if it existed",
        "metadata": {"engagement_score": 60, "published_at": None, "is_launch": False},
    }


def _batch_with_combo_last() -> list[dict]:
    # The combo candidate is deliberately appended LAST — its input
    # position must not matter to the outcome.
    signals = [_pure_traction_signal(e) for e in PURE_TRACTION_ENGAGEMENT_SCORES]
    signals.append(_combo_signal())
    return signals


def _new_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return Session()


def _created_source_urls(session) -> set[str]:
    return {
        e.source_url
        for e in session.scalars(select(Evidence)).all()
        if e.source_url
    }


@pytest.fixture
def db_session():
    session = _new_session()
    yield session
    session.close()


def test_combo_candidate_added_last_wins_the_cap_over_higher_engagement_pure_traction(db_session):
    result = process_raw_signals(db_session, _batch_with_combo_last(), engagement_threshold=50, max_new_opportunities_per_run=CAP)

    assert result["signals_new"] == len(PURE_TRACTION_ENGAGEMENT_SCORES) + 1
    assert result["candidates_created"] == CAP
    assert result["candidates_skipped_cap"] == 2  # 7 gate-passing candidates - cap of 5

    created_urls = _created_source_urls(db_session)

    # The purchase-intent + traction combo (engagement 60) must make the
    # cap despite being added last and having far lower raw engagement
    # than several pure-traction candidates.
    assert COMBO_URL in created_urls

    # The two lowest pre-ranked pure-traction candidates (80, 200) must be
    # the ones that fall out — proving ranking, not arrival order, decides.
    assert _pure_traction_url(80) not in created_urls
    assert _pure_traction_url(200) not in created_urls

    # The four higher pure-traction candidates survive.
    for engagement_score in (500, 1000, 3000, 6015):
        assert _pure_traction_url(engagement_score) in created_urls

    # All Signal rows persist regardless of cap outcome.
    assert len(db_session.scalars(select(Signal)).all()) == len(PURE_TRACTION_ENGAGEMENT_SCORES) + 1


def test_ranking_outcome_is_identical_across_shuffled_input_orders():
    orderings = [
        _batch_with_combo_last(),
        list(reversed(_batch_with_combo_last())),
        # Combo candidate moved to the FRONT this time, opposite of the
        # "added last" scenario above — outcome must still be identical.
        [_combo_signal()] + [_pure_traction_signal(e) for e in PURE_TRACTION_ENGAGEMENT_SCORES],
    ]

    outcomes = []
    for ordering in orderings:
        session = _new_session()
        result = process_raw_signals(session, ordering, engagement_threshold=50, max_new_opportunities_per_run=CAP)
        assert result["candidates_created"] == CAP
        assert result["candidates_skipped_cap"] == 2
        outcomes.append(_created_source_urls(session))
        session.close()

    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert COMBO_URL in outcomes[0]


def test_promoted_opportunities_keep_score_and_evidence_confidence_null(db_session):
    process_raw_signals(db_session, _batch_with_combo_last(), engagement_threshold=50, max_new_opportunities_per_run=CAP)

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == CAP
    for opportunity in opportunities:
        assert opportunity.score is None
        assert opportunity.evidence_confidence is None


def test_deterministic_tie_break_on_source_url_when_pre_rank_scores_are_equal(db_session):
    # Two candidates with identical evidence composition and engagement ->
    # identical pre-rank score. With a cap of exactly 1, only the
    # lexicographically smaller source_url may survive, deterministically.
    tied_signals = [
        {
            "source": "rss",
            "source_url": "https://reviewer.example/tie-zzz",
            "title": "Tied candidate Z",
            "content": "so annoying that this exact thing keeps happening",
            "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
        },
        {
            "source": "rss",
            "source_url": "https://reviewer.example/tie-aaa",
            "title": "Tied candidate A",
            "content": "so annoying that this exact thing keeps happening too",
            "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
        },
    ]

    result = process_raw_signals(db_session, tied_signals, max_new_opportunities_per_run=1)

    assert result["candidates_created"] == 1
    assert result["candidates_skipped_cap"] == 1
    assert _created_source_urls(db_session) == {"https://reviewer.example/tie-aaa"}
