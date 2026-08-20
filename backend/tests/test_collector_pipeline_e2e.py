"""End-to-end proof that the M2.1 pipeline is source-agnostic.

A Hacker News-shaped raw signal and an RSS-shaped raw signal are fed into
a single process_raw_signals() call together, alongside a non-matching
signal, to prove the pipeline applies identical, non-source-specific
logic to both: storage, candidate detection, Opportunity/Evidence
creation, dedupe, provenance, and pre-scoring NULL state.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes as routes_module
from app.collectors.pipeline import process_raw_signals
from app.db.session import Base, get_db
from app.main import app
from app.models.entities import Evidence, Opportunity, Signal

FULL_SCORE_FACTORS = {
    "demand_evidence": 10,
    "problem_severity": 10,
    "purchase_intent": 10,
    "market_growth": 10,
    "competition_gap": 10,
    "distribution_potential": 10,
    "unit_economics": 10,
    "recurring_potential": 10,
    "speed_to_validation": 10,
    "automation_scalability": 10,
    "defensibility": 10,
    "capital_efficiency": 10,
    "risk": 10,
}


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


def _hn_like_signal(source_url="https://news.ycombinator.com/item?id=42", engagement_score=120):
    """Shape matches app.collectors.hackernews.fetch_recent_signals() output."""
    return {
        "source": "hackernews",
        "source_url": source_url,
        "title": "Wish there was a tool for cross-provider LLM cost tracking",
        "content": "Wish there was a tool for cross-provider LLM cost tracking",
        "metadata": {"engagement_score": engagement_score, "published_at": 1735689600.0},
    }


def _rss_like_signal(source_url="https://www.producthunt.com/posts/example-tool"):
    """Shape matches app.collectors.rss.fetch_recent_signals() output."""
    return {
        "source": "rss",
        "source_url": source_url,
        "title": "Example Tool launches on Product Hunt",
        "content": "<p>does anyone know a tool that solves onboarding drop-off?</p>",
        "metadata": {"engagement_score": None, "published_at": 1735776000.0},
    }


def _boring_signal(source, source_url):
    return {
        "source": source,
        "source_url": source_url,
        "title": "Routine update",
        "content": "an entirely unremarkable status update",
        "metadata": {"engagement_score": 1, "published_at": None},
    }


def test_single_pipeline_call_processes_hackernews_and_rss_signals_through_identical_code_path(db_session):
    hn = _hn_like_signal()
    rss = _rss_like_signal()
    noise = _boring_signal("hackernews", "https://news.ycombinator.com/item?id=99")

    result = process_raw_signals(db_session, [hn, rss, noise])

    assert result == {
        "signals_seen": 3,
        "signals_new": 3,
        "signals_duplicate": 0,
        "candidates_created": 2,
        # M2.2: process_raw_signals() gained a secondary volume-cap
        # counter; well under MAX_NEW_OPPORTUNITIES_PER_RUN here.
        "candidates_skipped_cap": 0,
    }

    stored_signals = db_session.scalars(select(Signal)).all()
    assert len(stored_signals) == 3
    assert {s.source for s in stored_signals} == {"hackernews", "rss"}

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == 2
    for opp in opportunities:
        assert opp.status.value == "discovered"
        assert opp.score is None
        assert opp.evidence_confidence is None

    # Provenance stays correct per source when both are processed in the
    # same call — no cross-contamination between sources.
    evidence_by_source = {e.source: e for e in db_session.scalars(select(Evidence)).all()}
    assert evidence_by_source["hackernews"].source_url == hn["source_url"]
    assert evidence_by_source["rss"].source_url == rss["source_url"]


def test_non_matching_signal_in_mixed_batch_creates_no_opportunity(db_session):
    hn = _hn_like_signal(source_url="https://news.ycombinator.com/item?id=1")
    boring_rss = _boring_signal("rss", "https://www.producthunt.com/posts/boring")

    result = process_raw_signals(db_session, [hn, boring_rss])

    assert result["signals_new"] == 2
    assert result["candidates_created"] == 1

    sources_with_opportunity = {e.source for e in db_session.scalars(select(Evidence)).all()}
    assert sources_with_opportunity == {"hackernews"}
    assert db_session.scalars(select(Signal).where(Signal.source == "rss")).one() is not None


def test_dedupe_by_source_url_works_regardless_of_which_source_sent_it(db_session):
    dup_url = "https://news.ycombinator.com/item?id=777"
    hn = _hn_like_signal(source_url=dup_url)
    # A second raw signal claiming a *different* source but the same
    # source_url — dedupe must key off source_url, not source.
    duplicate_from_other_source = _rss_like_signal(source_url=dup_url)

    result = process_raw_signals(db_session, [hn, duplicate_from_other_source])

    assert result["signals_seen"] == 2
    assert result["signals_new"] == 1
    assert result["signals_duplicate"] == 1

    stored = db_session.scalars(select(Signal).where(Signal.source_url == dup_url)).all()
    assert len(stored) == 1
    assert stored[0].source == "hackernews"


def test_m2_1_signal_flows_through_m1_scoring_and_telegram_alert(client, monkeypatch):
    """M1 regression: an Opportunity created by the M2.1 pipeline must
    still flow correctly through the pre-existing M1 scoring endpoint
    and Telegram alert path (M2.1 signal -> Opportunity -> Evidence ->
    M1 scoring -> Telegram). Only Telegram send is mocked."""
    sent_messages = []

    async def _record_alert(message):
        sent_messages.append(message)
        return True

    monkeypatch.setattr(routes_module, "send_telegram_message", _record_alert)

    # Use the exact same in-memory DB the TestClient is wired to (via the
    # conftest `client` fixture's dependency override), so the Opportunity
    # created by the pipeline is visible through the API.
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        hn_signal = _hn_like_signal(source_url="https://news.ycombinator.com/item?id=555")
        result = process_raw_signals(db, [hn_signal])
        assert result["candidates_created"] == 1
        opportunity = db.scalars(select(Opportunity)).one()
        opportunity_id = opportunity.id
    finally:
        next(db_gen, None)  # drive the generator's `finally: db.close()`

    response = client.post(
        f"/api/opportunities/{opportunity_id}/score",
        json={"factors": FULL_SCORE_FACTORS, "evidence_confidence": 80},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["score"] == 100
    assert body["evidence_confidence"] == 80
    assert body["telegram_alert_sent"] is True
    assert len(sent_messages) == 1

    listed = client.get("/api/opportunities").json()
    scored = next(x for x in listed if x["id"] == opportunity_id)
    assert scored["status"] == "scored"
    assert scored["score"] == 100
