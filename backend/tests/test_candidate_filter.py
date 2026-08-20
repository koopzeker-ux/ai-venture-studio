import pytest

from app.services.candidate_filter import (
    EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_PRODUCT_LAUNCH,
    EVIDENCE_TYPE_PURCHASE_INTENT,
    EVIDENCE_TYPE_TRACTION,
    STRONG_EVIDENCE_TYPES,
    detect_candidates,
)


def _signal(title="", content="", engagement_score=None, is_launch=False):
    return {
        "source": "fictitious_source_alpha",
        "source_url": "https://example.com/x",
        "title": title,
        "content": content,
        "metadata": {"engagement_score": engagement_score, "published_at": None, "is_launch": is_launch},
    }


@pytest.mark.parametrize(
    "phrase",
    [
        "wish there was a tool for this",
        "does anyone know a tool that solves this",
        "it's so annoying that nothing does this well",
        "I'm paying for Zapier but it barely works",
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


@pytest.mark.parametrize(
    "phrase",
    [
        "I would pay for this right now",
        "shut up and take my money",
        "how much does this cost, need it today",
        "is there a paid plan for this yet",
    ],
)
def test_purchase_intent_keyword_triggers_match(phrase):
    candidates = detect_candidates(_signal(content=phrase))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PURCHASE_INTENT


@pytest.mark.parametrize(
    "phrase",
    [
        "looking for an alternative to this expensive SaaS",
        "is there a better alternative to this clunky tool",
        "can anyone recommend an alternative to this",
    ],
)
def test_alternative_seeking_keyword_triggers_match(phrase):
    candidates = detect_candidates(_signal(content=phrase))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_ALTERNATIVE_SEEKING


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


def test_product_launch_trigger_matches_when_is_launch_true():
    candidates = detect_candidates(_signal(content="just launched our new thing", is_launch=True))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PRODUCT_LAUNCH


def test_product_launch_trigger_does_not_match_when_is_launch_false():
    candidates = detect_candidates(_signal(content="just launched our new thing", is_launch=False))
    assert candidates == []


def test_product_launch_is_not_a_strong_evidence_type():
    assert EVIDENCE_TYPE_PRODUCT_LAUNCH not in STRONG_EVIDENCE_TYPES


@pytest.mark.parametrize(
    "evidence_type",
    [
        EVIDENCE_TYPE_PAIN_POINT,
        EVIDENCE_TYPE_PURCHASE_INTENT,
        EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
        EVIDENCE_TYPE_TRACTION,
    ],
)
def test_all_strong_types_are_classified_strong(evidence_type):
    assert evidence_type in STRONG_EVIDENCE_TYPES


def test_triggers_are_independent_multiple_can_match():
    candidates = detect_candidates(
        _signal(content="wish there was a tool for this", engagement_score=999, is_launch=True),
        engagement_threshold=50,
    )
    evidence_types = {c["evidence_type"] for c in candidates}
    assert evidence_types == {EVIDENCE_TYPE_PAIN_POINT, EVIDENCE_TYPE_TRACTION, EVIDENCE_TYPE_PRODUCT_LAUNCH}
    assert len(candidates) == 3


def test_no_match_returns_empty_list():
    candidates = detect_candidates(_signal(content="just a normal update with no signal"))
    assert candidates == []


def test_missing_engagement_score_does_not_trigger():
    candidates = detect_candidates(_signal(content="normal content", engagement_score=None))
    assert candidates == []
