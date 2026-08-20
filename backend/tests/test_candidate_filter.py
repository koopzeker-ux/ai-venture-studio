import pytest

from app.services.candidate_filter import (
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_TRACTION,
    detect_candidates,
)


def _signal(title="", content="", engagement_score=None):
    return {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/x",
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": None},
    }


@pytest.mark.parametrize(
    "phrase",
    [
        "wish there was a tool for this",
        "does anyone know a tool that solves this",
        "it's so annoying that nothing does this well",
        "I'm paying for Zapier but it barely works",
        "looking for an alternative to this expensive SaaS",
    ],
)
def test_pain_point_keyword_triggers_match(phrase):
    candidates = detect_candidates(_signal(content=phrase))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PAIN_POINT


def test_pain_point_trigger_checks_title_too():
    candidates = detect_candidates(_signal(title="wish there was an easier way", content="nothing special"))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PAIN_POINT


def test_engagement_trigger_matches_at_threshold():
    candidates = detect_candidates(_signal(content="ordinary content", engagement_score=50), engagement_threshold=50)
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_TRACTION


def test_engagement_trigger_does_not_match_below_threshold():
    candidates = detect_candidates(_signal(content="ordinary content", engagement_score=49), engagement_threshold=50)
    assert candidates == []


def test_engagement_trigger_respects_configurable_threshold():
    candidates = detect_candidates(_signal(content="ordinary content", engagement_score=120), engagement_threshold=200)
    assert candidates == []

    candidates = detect_candidates(_signal(content="ordinary content", engagement_score=120), engagement_threshold=100)
    assert len(candidates) == 1


def test_triggers_are_independent_both_can_match():
    candidates = detect_candidates(
        _signal(content="wish there was a tool for this", engagement_score=999),
        engagement_threshold=50,
    )
    evidence_types = {c["evidence_type"] for c in candidates}
    assert evidence_types == {EVIDENCE_TYPE_PAIN_POINT, EVIDENCE_TYPE_TRACTION}
    assert len(candidates) == 2


def test_no_match_returns_empty_list():
    candidates = detect_candidates(_signal(content="just a normal update with no signal"))
    assert candidates == []


def test_missing_engagement_score_does_not_trigger():
    candidates = detect_candidates(_signal(content="normal content", engagement_score=None))
    assert candidates == []
