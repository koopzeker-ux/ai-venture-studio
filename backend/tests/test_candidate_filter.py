import math

import pytest

from app.services.candidate_filter import (
    EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_PRODUCT_LAUNCH,
    EVIDENCE_TYPE_PURCHASE_INTENT,
    EVIDENCE_TYPE_TRACTION,
    STRONG_EVIDENCE_TYPES,
    compute_pre_rank_score,
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


@pytest.mark.parametrize(
    "phrase",
    [
        "we do this manually every single week",
        "is there a tool that can automate this for me",
    ],
)
def test_m3_4_pain_point_phrase_extension_matches(phrase):
    candidates = detect_candidates(_signal(content=phrase))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PAIN_POINT


@pytest.mark.parametrize(
    "phrase",
    [
        "looking for a tool that handles invoicing",
        "looking for software to manage bookings",
        "where can I buy something like this",
        "I need a product that solves onboarding",
        "can anyone recommend a service for this",
    ],
)
def test_m3_4_purchase_intent_phrase_extension_matches(phrase):
    candidates = detect_candidates(_signal(content=phrase))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_PURCHASE_INTENT


def test_m3_4_alternative_seeking_phrase_extension_matches():
    candidates = detect_candidates(_signal(content="looking to replace this clunky system"))
    assert len(candidates) == 1
    assert candidates[0]["evidence_type"] == EVIDENCE_TYPE_ALTERNATIVE_SEEKING


# ===========================================================================
# LEAD fix (M3.4 pre-review, precision): "this process just takes hours to
# finish"/"I'm struggling with our current setup"/"so frustrated with this
# workflow"/"I hate using this clunky dashboard"/"why is there no simple way
# to do this" (PAIN_POINT), "willing to pay for a proper solution"
# (PURCHASE_INTENT), and "what's a good alternative to this expensive
# tool"/"any alternatives to this out there"/"what do you use instead of
# this"/"looking for a replacement for our current stack"/"we are switching
# from this to something cheaper" (ALTERNATIVE_SEEKING) were REMOVED from
# BUILDER's own positive-match parametrize lists above -- the underlying
# phrases were removed from candidate_filter.py after adversarial testing
# proved each one also fires on ordinary, non-commercial sentences (see the
# module-level comments on PAIN_POINT_PHRASES/PURCHASE_INTENT_PHRASES/
# ALTERNATIVE_SEEKING_PHRASES for the specific counter-examples). This is
# not a weakened assertion -- it is the direct, intended consequence of a
# precision fix, verified below as the new expected (negative) behavior.
# ===========================================================================

@pytest.mark.parametrize(
    "adversarial_text",
    [
        "I am struggling with my mental health lately",
        "I am frustrated with my kids not listening",
        "I hate using public transport in the rain",
        "why is there no update to this app yet",
        "this movie takes hours to get through",
    ],
)
def test_removed_pain_point_phrases_no_longer_false_positive(adversarial_text):
    candidates = detect_candidates(_signal(content=adversarial_text))
    assert candidates == []


def test_removed_purchase_intent_phrase_no_longer_false_positives():
    candidates = detect_candidates(_signal(content="willing to pay my taxes on time this year"))
    assert candidates == []


@pytest.mark.parametrize(
    "adversarial_text",
    [
        "what is a good alternative to democracy",
        "what are some alternatives to war",
        "I walked instead of driving today",
        "looking for a replacement for my missing tooth",
        "we are switching from Windows to Linux for gaming",
    ],
)
def test_removed_alternative_seeking_phrases_no_longer_false_positive(adversarial_text):
    candidates = detect_candidates(_signal(content=adversarial_text))
    assert candidates == []


@pytest.mark.parametrize(
    "safe_text",
    [
        "looking for an alternative to this expensive SaaS",
        "is there a better alternative to this clunky tool",
        "can anyone recommend an alternative to this",
        "looking to replace this clunky system",
    ],
)
def test_original_and_kept_alternative_seeking_phrases_still_match(safe_text):
    """Confirms the precision fix removed only the unanchored additions --
    the three original, already-anchored phrases and the one kept M3.4
    addition are unaffected."""
    candidates = detect_candidates(_signal(content=safe_text))
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


def _candidates(*evidence_types):
    return [{"evidence_type": t, "trigger_detail": "x"} for t in evidence_types]


def test_pre_rank_base_score_is_two_times_evidence_type_count():
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_PAIN_POINT), None) == round(2.0 * 1 + 1.0, 3)
    assert compute_pre_rank_score(
        _candidates(EVIDENCE_TYPE_PAIN_POINT, EVIDENCE_TYPE_ALTERNATIVE_SEEKING), None
    ) == round(2.0 * 2 + 1.0 + 1.0, 3)


def test_pre_rank_purchase_intent_bonus():
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_PURCHASE_INTENT), None) == round(2.0 + 1.5, 3)


def test_pre_rank_alternative_seeking_bonus():
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_ALTERNATIVE_SEEKING), None) == round(2.0 + 1.0, 3)


def test_pre_rank_pain_point_bonus():
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_PAIN_POINT), None) == round(2.0 + 1.0, 3)


def test_pre_rank_launch_plus_traction_bonus_requires_both():
    launch_and_traction = compute_pre_rank_score(
        _candidates(EVIDENCE_TYPE_PRODUCT_LAUNCH, EVIDENCE_TYPE_TRACTION), 0
    )
    traction_only = compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), 0)
    launch_only = compute_pre_rank_score(_candidates(EVIDENCE_TYPE_PRODUCT_LAUNCH), None)

    # base(2 types)=4.0 + launch+traction bonus 0.5 + engagement(0)=log10(1)=0 -> 4.5
    assert launch_and_traction == 4.5
    # base(1 type)=2.0 + engagement(0)=0 -> 2.0, no launch+traction bonus without launch
    assert traction_only == 2.0
    # base(1 type)=2.0, no traction present so no bonus
    assert launch_only == 2.0


def test_pre_rank_traction_engagement_term_uses_log10_capped_at_four():
    # engagement=99 -> log10(100)=2.0 exactly -> +1.0
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), 99) == round(2.0 + 1.0, 3)
    # engagement=999 -> log10(1000)=3.0 exactly -> +1.5
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), 999) == round(2.0 + 1.5, 3)
    # extremely high engagement is capped at log10 component == 4.0 -> +2.0 max
    huge = compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), 10_000_000)
    assert huge == 4.0


def test_pre_rank_engagement_term_only_applies_with_traction_present():
    # purchase_intent alone with an engagement_score passed in must not pick
    # up the log10 bonus meant for traction_signal.
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_PURCHASE_INTENT), 999) == round(2.0 + 1.5, 3)


def test_pre_rank_rounds_to_three_decimals():
    engagement = 500
    expected = round(2.0 + min(math.log10(engagement + 1), 4.0) * 0.5, 3)
    result = compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), engagement)
    assert result == expected
    assert result == round(result, 3)


def test_pre_rank_invalid_engagement_score_is_ignored_safely():
    # No engagement_score at all — traction bonus doesn't error, just skips.
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), None) == 2.0
    # Negative engagement (shouldn't occur post-normalize, but must not crash).
    assert compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), -5) == 2.0


def test_pre_rank_full_combo_purchase_intent_plus_traction_beats_pure_traction_max():
    combo = compute_pre_rank_score(
        _candidates(EVIDENCE_TYPE_PURCHASE_INTENT, EVIDENCE_TYPE_TRACTION), 60
    )
    pure_traction_at_cap = compute_pre_rank_score(_candidates(EVIDENCE_TYPE_TRACTION), 10_000_000)
    assert combo > pure_traction_at_cap
