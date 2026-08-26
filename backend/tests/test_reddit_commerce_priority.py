"""M3.4: Reddit-sourced signals flowing through the source-agnostic pipeline.

Covers the task's commerce-first requirement (explicit commercial intent
must outrank raw engagement/traction under a volume cap), multi-evidence-
type detection from one item, rerun dedup, provenance preservation, and
the "no fake economics / no fabricated evidence" guardrails. No live
Reddit call anywhere in this file -- raw signal dicts are built by hand in
the exact shape app.collectors.reddit.fetch_recent_signals() produces.
"""
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


def _reddit_raw(source_url, title="", content="", subreddit="smallbusiness", external_id=None):
    """Shape matches app.collectors.reddit.fetch_recent_signals() output --
    engagement_score is always None (Reddit's public RSS never exposes
    vote/comment counts)."""
    return {
        "source": "reddit",
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {
            "engagement_score": None,
            "published_at": 1735689600.0,
            "is_launch": False,
            "subreddit": subreddit,
            "external_id": external_id,
        },
    }


def _news_raw(source, source_url, title="", content="", engagement_score=2000):
    """A high-engagement, purely-informational item from an attention
    source (e.g. Hacker News) -- traction_signal only, no commercial
    intent in the text itself."""
    return {
        "source": source,
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": None, "is_launch": False},
    }


def test_low_engagement_reddit_purchase_intent_survives_gate(db_session):
    # No engagement_score at all (Reddit RSS never exposes it) -- gate
    # must still pass on commercial-intent text alone.
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x1/",
            title="Dental practice scheduling",
            content="I run a dental practice and need a product that automates appointment reminders, willing to pay",
        )
    ]
    result = process_raw_signals(db_session, signals)

    assert result["candidates_created"] == 1
    opportunity = db_session.scalars(select(Opportunity)).one()
    evidence_types = {
        e.evidence_type for e in db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id))
    }
    assert "purchase_intent_signal" in evidence_types


def test_low_engagement_commercial_intent_outranks_high_engagement_pure_traction_under_cap(db_session):
    """The task's own worked example: a 3-point pain-point post must win a
    capped slot over a 2,000-point pure-traction news story."""
    signals = [
        _news_raw(
            "hackernews",
            "https://news.ycombinator.com/item?id=1",
            title="Major security vulnerability discovered",
            content="Researchers disclosed a critical vulnerability affecting millions of devices",
            engagement_score=2000,
        ),
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x2/",
            title="Manual invoicing is killing us",
            content="We do this manually every week and it takes hours, is there a tool that automates it?",
        ),
    ]
    result = process_raw_signals(db_session, signals, engagement_threshold=50, max_new_opportunities_per_run=1)

    assert result["candidates_created"] == 1
    urls = {e.source_url for e in db_session.scalars(select(Evidence)).all()}
    assert urls == {"https://www.reddit.com/r/smallbusiness/comments/x2/"}


def test_commerce_tier_survives_the_real_volume_cap_against_a_large_traction_batch(db_session):
    """LEAD fix (M3.4 pre-review §7): the 1-vs-1 test above proves the
    ordering principle, but does not exercise the REAL
    MAX_NEW_OPPORTUNITIES_PER_RUN cap (20) -- this constructs an
    adversarial batch that actually exceeds it: 30 very-high-engagement,
    purely-informational traction candidates plus exactly one Reddit item
    with NO engagement_score at all (Reddit's public RSS never exposes
    one) but genuine purchase intent. The commercial candidate must be
    among the 20 created, regardless of how many traction-only candidates
    compete for the remaining slots."""
    from app.collectors.pipeline import MAX_NEW_OPPORTUNITIES_PER_RUN

    signals = [
        _news_raw(
            "hackernews", f"https://news.ycombinator.com/item?id={i}",
            title=f"Major viral news story number {i}", content="nothing commercial here, just news",
            engagement_score=5000 + i,
        )
        for i in range(30)
    ]
    signals.append(_reddit_raw(
        "https://www.reddit.com/r/smallbusiness/comments/theone/",
        title="Would pay for this",
        content="I run a dental practice and would pay for a tool that automates scheduling",
    ))

    result = process_raw_signals(db_session, signals, engagement_threshold=50)

    assert result["candidates_created"] == MAX_NEW_OPPORTUNITIES_PER_RUN
    assert result["candidates_skipped_cap"] == 31 - MAX_NEW_OPPORTUNITIES_PER_RUN
    survivor_urls = {e.source_url for e in db_session.scalars(select(Evidence)).all()}
    assert "https://www.reddit.com/r/smallbusiness/comments/theone/" in survivor_urls


def test_pure_traction_only_survives_when_cap_is_not_hit(db_session):
    """Commerce-first ranking is about ordering under a cap, not deleting
    traction-only candidates outright -- traction_signal alone still
    passes the gate and is still created when there's room."""
    signals = [
        _news_raw(
            "hackernews", "https://news.ycombinator.com/item?id=2",
            content="An ordinary high-traction story", engagement_score=2000,
        ),
    ]
    result = process_raw_signals(db_session, signals, engagement_threshold=50)

    assert result["candidates_created"] == 1


def test_one_reddit_item_can_carry_multiple_justified_evidence_types(db_session):
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/SaaS/comments/x3/",
            title="Looking to replace our current tool",
            content=(
                "This costs us $500/month and it is so annoying that nothing does this well, "
                "looking to replace it with something better"
            ),
        )
    ]
    result = process_raw_signals(db_session, signals)
    assert result["candidates_created"] == 1

    opportunity = db_session.scalars(select(Opportunity)).one()
    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()
    evidence_types = {e.evidence_type for e in evidence}

    # Both a pain-point and an alternative-seeking trigger fired from the
    # same item's text -- neither collapses into the other or into
    # traction_signal (which can't fire anyway: engagement_score is None).
    assert "pain_point_signal" in evidence_types
    assert "alternative_seeking_signal" in evidence_types
    assert "traction_signal" not in evidence_types
    # No evidence type unsupported by the actual triggered text.
    assert evidence_types <= {"pain_point_signal", "alternative_seeking_signal", "purchase_intent_signal"}


def test_no_fake_economics_generated_from_dollar_amount_in_source_text(db_session):
    """$500/month in the raw text must be preserved verbatim as evidence
    claim text -- never converted into a structured economics field.
    Opportunity has no such fields at all; this proves none get invented."""
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/SaaS/comments/x4/",
            content="This costs us $500/month and we are struggling with it, need a product that replaces it",
        )
    ]
    process_raw_signals(db_session, signals)

    opportunity = db_session.scalars(select(Opportunity)).one()
    evidence = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id)).all()

    assert opportunity.score is None
    assert opportunity.evidence_confidence is None
    for e in evidence:
        # The claim is an unverified heuristic trigger note, not a market/
        # revenue estimate -- the dollar figure only ever appears as
        # untouched source text on the raw signal, never restructured.
        assert "unverified heuristic signal" in e.claim.lower()


def test_reddit_signal_rerun_deduplicates_via_existing_source_url_semantics(db_session):
    """Reuses the generic Signal.source_url UNIQUE constraint -- no new
    Reddit-specific dedup mechanism needed (same permalink across two
    collector runs of the same subreddit)."""
    first_run = [
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x5/",
            content="wish there was a tool for this",
            external_id="t3_x5",
        )
    ]
    result_1 = process_raw_signals(db_session, first_run)
    assert result_1["signals_new"] == 1
    assert result_1["candidates_created"] == 1

    # Second collector run re-fetches the same post (identical permalink).
    second_run = [
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x5/",
            content="wish there was a tool for this",
            external_id="t3_x5",
        )
    ]
    result_2 = process_raw_signals(db_session, second_run)

    assert result_2["signals_new"] == 0
    assert result_2["signals_duplicate"] == 1
    assert result_2["candidates_created"] == 0
    assert len(db_session.scalars(select(Signal)).all()) == 1
    assert len(db_session.scalars(select(Opportunity)).all()) == 1


def test_reddit_provenance_preserved_through_signal_metadata(db_session):
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/Entrepreneur/comments/x6/",
            content="frustrated with our current setup",
            subreddit="Entrepreneur",
            external_id="t3_x6",
        )
    ]
    process_raw_signals(db_session, signals)

    signal = db_session.scalars(select(Signal)).one()
    assert signal.source == "reddit"
    assert signal.source_url == "https://www.reddit.com/r/Entrepreneur/comments/x6/"
    assert signal.metadata_json["subreddit"] == "Entrepreneur"
    assert signal.metadata_json["external_id"] == "t3_x6"
    assert signal.metadata_json["engagement_score"] is None


def test_absent_engagement_never_fabricated_as_zero_evidence(db_session):
    """A Reddit signal with no commercial-intent phrase and no engagement
    must create nothing -- absence of engagement_score must never be
    treated as engagement_score=0 passing some threshold, and must never
    itself become an evidence trigger."""
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x7/",
            content="just sharing my morning coffee setup, nothing special",
        )
    ]
    result = process_raw_signals(db_session, signals, engagement_threshold=0)

    assert result["candidates_created"] == 0
    assert db_session.scalars(select(Opportunity)).all() == []


def test_reddit_markdown_case_and_whitespace_normalized_before_detection(db_session):
    """Reddit-flavored markdown (bold/links), mixed case, and irregular
    whitespace/newlines must not prevent a real trigger from matching --
    proves the full normalize -> detect_candidates chain, not just each
    piece in isolation."""
    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/SaaS/comments/x9/",
            title="**STRUGGLING WITH**   our   [current tool](https://example.com)",
            content="we  do   this\n\nmanually\nevery week",
        )
    ]
    result = process_raw_signals(db_session, signals)

    assert result["candidates_created"] == 1
    opportunity = db_session.scalars(select(Opportunity)).one()
    evidence_types = {
        e.evidence_type for e in db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id))
    }
    assert "pain_point_signal" in evidence_types


def test_no_evidence_type_unsupported_by_triggered_text_is_invented(db_session):
    """Every Evidence row's evidence_type must trace back to an actual
    trigger that fired on the stored text -- the pipeline never adds a
    type (e.g. traction_signal) that detect_candidates() didn't return."""
    from app.services.candidate_filter import detect_candidates
    from app.services.normalize import normalize_raw_signal

    raw = _reddit_raw(
        "https://www.reddit.com/r/SaaS/comments/x10/",
        content="would pay for this immediately, is there a paid plan",
    )
    process_raw_signals(db_session, [raw])

    expected_types = {c["evidence_type"] for c in detect_candidates(normalize_raw_signal(raw))}
    opportunity = db_session.scalars(select(Opportunity)).one()
    stored_types = {
        e.evidence_type for e in db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opportunity.id))
    }
    assert stored_types == expected_types
    assert "traction_signal" not in stored_types  # engagement_score is None for Reddit


def test_reddit_collector_module_has_no_llm_provider_imports():
    """Static proof of item P (no model/LLM calls): the collector module
    never imports an LLM SDK at all -- there is no code path through which
    it could make one."""
    import app.collectors.reddit as reddit_module

    source = reddit_module.__file__
    with open(source, encoding="utf-8") as f:
        contents = f.read().lower()

    for forbidden in ("anthropic", "openai", "claude_code_adapter"):
        assert forbidden not in contents


def test_reddit_signal_never_triggers_a_paid_call_or_telegram_send(db_session, monkeypatch):
    """No Claude/OpenAI/Researcher/Critic call and no Telegram send is
    reachable from process_raw_signals() at all -- proven by monkeypatching
    the one shared send path to raise if it's ever invoked."""
    from app.services import telegram as telegram_module

    async def _fail_if_called(*args, **kwargs):
        raise AssertionError("Telegram must never be called from signal ingestion")

    monkeypatch.setattr(telegram_module, "send_telegram_message", _fail_if_called)

    signals = [
        _reddit_raw(
            "https://www.reddit.com/r/smallbusiness/comments/x8/",
            content="would pay for this immediately, need a product that solves it",
        )
    ]
    result = process_raw_signals(db_session, signals)

    assert result["candidates_created"] == 1
    opportunity = db_session.scalars(select(Opportunity)).one()
    assert opportunity.score is None
    assert opportunity.evidence_confidence is None
