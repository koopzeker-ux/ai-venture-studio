"""Source-agnostic heuristic candidate detection.

Operates only on normalized signal fields (title/content/metadata) —
no knowledge of any specific source. These are cheap triage heuristics,
NOT the Opportunity Score (see CLAUDE.md §12: Evidence Confidence is a
separate axis from Opportunity Score).
"""
import re

DEFAULT_ENGAGEMENT_THRESHOLD = 50

EVIDENCE_TYPE_PAIN_POINT = "pain_point_signal"
EVIDENCE_TYPE_TRACTION = "traction_signal"

PAIN_POINT_PHRASES = [
    "wish there was",
    "does anyone know a tool",
    "so annoying that",
    "looking for an alternative to",
]

_PAIN_POINT_PATTERNS = [re.compile(re.escape(phrase), re.IGNORECASE) for phrase in PAIN_POINT_PHRASES]
_PAYING_FOR_BUT_PATTERN = re.compile(r"paying for .+? but", re.IGNORECASE)


def _matched_pain_point_phrase(text: str) -> str | None:
    for pattern in _PAIN_POINT_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)

    match = _PAYING_FOR_BUT_PATTERN.search(text)
    if match:
        return match.group(0)

    return None


def detect_candidates(normalized_signal: dict, engagement_threshold: int = DEFAULT_ENGAGEMENT_THRESHOLD) -> list[dict]:
    """Return a list of triggered heuristic candidates (0, 1, or 2 entries).

    Each entry: {"evidence_type": str, "trigger_detail": str}
    Triggers are independent — a signal can match either, both, or neither.
    """
    candidates: list[dict] = []
    text = f"{normalized_signal.get('title') or ''} {normalized_signal.get('content') or ''}"

    phrase = _matched_pain_point_phrase(text)
    if phrase:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_PAIN_POINT,
            "trigger_detail": f"matched phrase: '{phrase}'",
        })

    engagement_score = (normalized_signal.get("metadata") or {}).get("engagement_score")
    if engagement_score is not None and engagement_score >= engagement_threshold:
        candidates.append({
            "evidence_type": EVIDENCE_TYPE_TRACTION,
            "trigger_detail": f"engagement_score={engagement_score} >= threshold={engagement_threshold}",
        })

    return candidates
