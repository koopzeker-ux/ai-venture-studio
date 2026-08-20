"""Source-agnostic heuristic candidate detection.

Operates only on normalized signal fields (title/content/metadata) —
no knowledge of any specific source. These are cheap triage heuristics,
NOT the Opportunity Score (see CLAUDE.md §12: Evidence Confidence is a
separate axis from Opportunity Score).

detect_candidates() returns every trigger that fired. It does NOT decide
which combinations are allowed to create an Opportunity — that promotion
gate lives in pipeline.py, using the STRONG_EVIDENCE_TYPES classification
below. product_launch_signal is deliberately excluded from that set: a
launch is context, not evidence of demand.
"""
import math
import re

DEFAULT_ENGAGEMENT_THRESHOLD = 50

EVIDENCE_TYPE_PAIN_POINT = "pain_point_signal"
EVIDENCE_TYPE_PURCHASE_INTENT = "purchase_intent_signal"
EVIDENCE_TYPE_ALTERNATIVE_SEEKING = "alternative_seeking_signal"
EVIDENCE_TYPE_TRACTION = "traction_signal"
EVIDENCE_TYPE_PRODUCT_LAUNCH = "product_launch_signal"

# Strong = independently credible evidence of demand/pain/traction.
# product_launch_signal is intentionally NOT here — a launch alone is not
# promotion-worthy (see CLAUDE.md M2.2 task: Product Hunt marks ~50/80
# live signals as is_launch=True, which would otherwise flood the pipeline
# with candidates purely because products exist).
STRONG_EVIDENCE_TYPES = {
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_PURCHASE_INTENT,
    EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
    EVIDENCE_TYPE_TRACTION,
}

PAIN_POINT_PHRASES = [
    "wish there was",
    "does anyone know a tool",
    "so annoying that",
]

PURCHASE_INTENT_PHRASES = [
    "would pay for",
    "take my money",
    "how much does this cost",
    "is there a paid plan",
]

ALTERNATIVE_SEEKING_PHRASES = [
    "looking for an alternative to",
    "is there a better alternative to",
    "recommend an alternative to",
]

_PAIN_POINT_PATTERNS = [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in PAIN_POINT_PHRASES]
_PURCHASE_INTENT_PATTERNS = [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in PURCHASE_INTENT_PHRASES]
_ALTERNATIVE_SEEKING_PATTERNS = [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in ALTERNATIVE_SEEKING_PHRASES]
_PAYING_FOR_BUT_PATTERN = re.compile(r"paying for .+? but", re.IGNORECASE)


def _first_match(patterns: list[re.Pattern], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _matched_pain_point_phrase(text: str) -> str | None:
    phrase = _first_match(_PAIN_POINT_PATTERNS, text)
    if phrase:
        return phrase

    match = _PAYING_FOR_BUT_PATTERN.search(text)
    if match:
        return match.group(0)

    return None


def detect_candidates(normalized_signal: dict, engagement_threshold: int = DEFAULT_ENGAGEMENT_THRESHOLD) -> list[dict]:
    """Return every triggered heuristic candidate (0..5 entries).

    Each entry: {"evidence_type": str, "trigger_detail": str}
    Triggers are independent — a signal can match any combination, including
    none. This function does not gate promotion to Opportunity; see
    STRONG_EVIDENCE_TYPES and pipeline.py's candidate gate for that.
    """
    candidates: list[dict] = []
    text = f"{normalized_signal.get('title') or ''} {normalized_signal.get('content') or ''}"
    metadata = normalized_signal.get("metadata") or {}

    pain_phrase = _matched_pain_point_phrase(text)
    if pain_phrase:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_PAIN_POINT,
            "trigger_detail": f"matched phrase: '{pain_phrase}'",
        })

    purchase_phrase = _first_match(_PURCHASE_INTENT_PATTERNS, text)
    if purchase_phrase:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_PURCHASE_INTENT,
            "trigger_detail": f"matched phrase: '{purchase_phrase}'",
        })

    alternative_phrase = _first_match(_ALTERNATIVE_SEEKING_PATTERNS, text)
    if alternative_phrase:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
            "trigger_detail": f"matched phrase: '{alternative_phrase}'",
        })

    engagement_score = metadata.get("engagement_score")
    if engagement_score is not None and engagement_score >= engagement_threshold:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_TRACTION,
            "trigger_detail": f"engagement_score={engagement_score} >= threshold={engagement_threshold}",
        })

    if metadata.get("is_launch") is True:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_PRODUCT_LAUNCH,
            "trigger_detail": "is_launch=True",
        })

    return candidates


def compute_pre_rank_score(candidates: list[dict], engagement_score: int | None) -> float:
    """Deterministic commercial pre-ranking of a gate-passing candidate.

    Answers only "should we look into this candidate first?" — this is NOT
    Opportunity Score and NOT Evidence Confidence (CLAUDE.md §12: those are
    separate axes, set only during scoring, never here).

    score = 2.0 * (distinct evidence_types present)
        + 1.5 if purchase_intent_signal present
        + 1.0 if alternative_seeking_signal present
        + 1.0 if pain_point_signal present
        + 0.5 if product_launch_signal AND traction_signal both present
        + min(log10(engagement_score + 1), 4.0) * 0.5
              if traction_signal present and engagement_score is valid (>= 0)
    Rounded to 3 decimals.
    """
    evidence_types = {candidate["evidence_type"] for candidate in candidates}
    score = 2.0 * len(evidence_types)

    if EVIDENCE_TYPE_PURCHASE_INTENT in evidence_types:
        score += 1.5
    if EVIDENCE_TYPE_ALTERNATIVE_SEEKING in evidence_types:
        score += 1.0
    if EVIDENCE_TYPE_PAIN_POINT in evidence_types:
        score += 1.0
    if EVIDENCE_TYPE_PRODUCT_LAUNCH in evidence_types and EVIDENCE_TYPE_TRACTION in evidence_types:
        score += 0.5

    if EVIDENCE_TYPE_TRACTION in evidence_types and engagement_score is not None and engagement_score >= 0:
        score += min(math.log10(engagement_score + 1), 4.0) * 0.5

    return round(score, 3)
