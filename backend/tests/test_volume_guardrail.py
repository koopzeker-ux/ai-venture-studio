"""Independent REVIEWER coverage for the M2.2 volume guardrail.

MAX_NEW_OPPORTUNITIES_PER_RUN is a secondary safety net only — the
candidate gate (STRONG_EVIDENCE_TYPES) is the primary quality filter.
This file proves the cap itself: exceeding it with genuinely strong
candidates must still bound the number of Opportunities created per run,
report the overflow via candidates_skipped_cap, and never drop the
underlying Signal rows.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.collectors.pipeline import MAX_NEW_OPPORTUNITIES_PER_RUN, process_raw_signals
from app.db.session import Base
from app.models.entities import Opportunity, Signal

OVERFLOW_COUNT = 5


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


def _strong_pain_point_signal(index):
    return {
        "source": "rss",
        "source_url": f"https://www.producthunt.com/posts/strong-{index}",
        "title": f"Frustrated user post {index}",
        "content": "wish there was a tool for this exact problem",
        "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
    }


def test_volume_cap_bounds_opportunities_at_the_real_default_constant(db_session):
    """Uses the actual production default (no override), so this proves
    the real MAX_NEW_OPPORTUNITIES_PER_RUN=20 behavior, not a
    test-configured stand-in value."""
    total_signals = MAX_NEW_OPPORTUNITIES_PER_RUN + OVERFLOW_COUNT
    raw_signals = [_strong_pain_point_signal(i) for i in range(total_signals)]

    result = process_raw_signals(db_session, raw_signals)

    assert result["signals_new"] == total_signals
    assert result["candidates_created"] == MAX_NEW_OPPORTUNITIES_PER_RUN
    assert result["candidates_skipped_cap"] == OVERFLOW_COUNT

    opportunities = db_session.scalars(select(Opportunity)).all()
    assert len(opportunities) == MAX_NEW_OPPORTUNITIES_PER_RUN

    # Every signal is still stored, cap or no cap — the cap only bounds
    # Opportunity promotion, never raw signal capture.
    signals = db_session.scalars(select(Signal)).all()
    assert len(signals) == total_signals


def test_volume_cap_is_configurable_via_explicit_override(db_session):
    max_new = 4
    overflow = 3
    total_signals = max_new + overflow
    raw_signals = [_strong_pain_point_signal(i) for i in range(total_signals)]

    result = process_raw_signals(db_session, raw_signals, max_new_opportunities_per_run=max_new)

    assert result["candidates_created"] == max_new
    assert result["candidates_skipped_cap"] == overflow
    assert len(db_session.scalars(select(Opportunity)).all()) == max_new
    assert len(db_session.scalars(select(Signal)).all()) == total_signals
