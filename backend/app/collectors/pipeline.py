"""Source-agnostic Market Intelligence pipeline.

raw signal -> normalize -> dedupe -> candidate detection -> Signal -> Opportunity + Evidence

No source-specific logic lives here (see CLAUDE.md M2.1 task). This module
only knows the generic raw signal shape handled by app.services.normalize.
"""
import hashlib
import re

from sqlalchemy.exc import IntegrityError

from app.models.entities import Evidence, Opportunity, Signal
from app.services.candidate_filter import DEFAULT_ENGAGEMENT_THRESHOLD, detect_candidates
from app.services.normalize import normalize_raw_signal

_SLUG_NOISE_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, uniqueness_key: str) -> str:
    base = _SLUG_NOISE_RE.sub("-", text.lower()).strip("-")[:60] or "signal"
    suffix = hashlib.sha1(uniqueness_key.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"


def _build_thesis(normalized: dict, candidates: list[dict]) -> str:
    triggers = ", ".join(candidate["evidence_type"] for candidate in candidates)
    provenance = normalized["source_url"] or "no source_url provided"
    return (
        f"Unverified heuristic triage signal ({triggers}) from source '{normalized['source']}'. "
        f"Provenance: {provenance}. "
        "This is a low-confidence triage flag, not a validated opportunity — "
        "requires research before scoring."
    )


def _create_opportunity_with_evidence(db, normalized: dict, candidates: list[dict]) -> Opportunity:
    title = (normalized["title"] or normalized["content"] or "Untitled signal")[:250]
    opportunity = Opportunity(
        slug=_slugify(title, normalized["source_url"] or normalized["source"]),
        title=title,
        thesis=_build_thesis(normalized, candidates),
        score=None,
        evidence_confidence=None,
    )
    db.add(opportunity)
    db.flush()

    for candidate in candidates:
        evidence = Evidence(
            opportunity_id=opportunity.id,
            claim=(
                f"Unverified heuristic signal ({candidate['evidence_type']}): "
                f"{candidate['trigger_detail']}"
            ),
            evidence_type=candidate["evidence_type"],
            source=normalized["source"],
            source_url=normalized["source_url"] or None,
            confidence=0.3,
            independently_confirmed=False,
        )
        db.add(evidence)

    return opportunity


def process_raw_signals(db, raw_signals: list[dict], engagement_threshold: int = DEFAULT_ENGAGEMENT_THRESHOLD) -> dict:
    signals_seen = 0
    signals_new = 0
    signals_duplicate = 0
    candidates_created = 0

    for raw in raw_signals:
        normalized = normalize_raw_signal(raw)
        signals_seen += 1

        signal = Signal(
            source=normalized["source"],
            source_url=normalized["source_url"] or None,
            title=normalized["title"] or None,
            content=normalized["content"],
            metadata_json=normalized["metadata"],
        )
        db.add(signal)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            signals_duplicate += 1
            continue

        signals_new += 1

        candidates = detect_candidates(normalized, engagement_threshold=engagement_threshold)
        if candidates:
            _create_opportunity_with_evidence(db, normalized, candidates)
            db.commit()
            candidates_created += 1

    return {
        "signals_seen": signals_seen,
        "signals_new": signals_new,
        "signals_duplicate": signals_duplicate,
        "candidates_created": candidates_created,
    }
