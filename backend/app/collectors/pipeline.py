"""Source-agnostic Market Intelligence pipeline.

raw signal -> normalize -> dedupe -> candidate detection -> Signal -> Opportunity + Evidence

No source-specific logic lives here (see CLAUDE.md M2.1 task). This module
only knows the generic raw signal shape handled by app.services.normalize.
"""
import hashlib
import re

from sqlalchemy.exc import IntegrityError

from app.models.entities import Evidence, Opportunity, Signal
from app.services.candidate_filter import (
    DEFAULT_ENGAGEMENT_THRESHOLD,
    EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_PURCHASE_INTENT,
    STRONG_EVIDENCE_TYPES,
    compute_pre_rank_score,
    detect_candidates,
)
from app.services.normalize import normalize_raw_signal

_SLUG_NOISE_RE = re.compile(r"[^a-z0-9]+")

# Secondary safety net only. The candidate gate (passes_candidate_gate) is
# the primary quality filter — this cap must never be used to paper over
# weak candidate detection, only to bound run size once genuinely strong
# signals arrive in volume.
MAX_NEW_OPPORTUNITIES_PER_RUN = 20


def passes_candidate_gate(evidence_types: set[str]) -> bool:
    """A signal is only promoted to an Opportunity if at least one STRONG
    trigger fired. product_launch_signal alone never passes this gate — a
    launch is context, not standalone evidence of demand."""
    return bool(evidence_types & STRONG_EVIDENCE_TYPES)


# M3.4: commerce-first ranking tier. Explicit commercial-intent evidence
# (pain/purchase/alternative-seeking) must outrank pure engagement/traction
# when a run's volume cap forces a choice between candidates — a 3-upvote
# post with genuine purchase intent should win a slot over a 2,000-point
# pure-traction news story. Deliberately NOT folded into
# STRONG_EVIDENCE_TYPES/compute_pre_rank_score in candidate_filter.py:
# that set still governs promotion (the *gate*), unchanged; this tier only
# reorders *which gate-passing candidates get created first* once the cap
# is hit. traction_signal/product_launch_signal are Tier 2 by omission.
_TIER_1_EVIDENCE_TYPES = {
    EVIDENCE_TYPE_PAIN_POINT,
    EVIDENCE_TYPE_PURCHASE_INTENT,
    EVIDENCE_TYPE_ALTERNATIVE_SEEKING,
}


def _commerce_tier(evidence_types: set[str]) -> int:
    """1 if any Tier-1 commercial-intent evidence type is present, else 0."""
    return 1 if evidence_types & _TIER_1_EVIDENCE_TYPES else 0


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


def process_raw_signals(
    db,
    raw_signals: list[dict],
    engagement_threshold: int = DEFAULT_ENGAGEMENT_THRESHOLD,
    max_new_opportunities_per_run: int = MAX_NEW_OPPORTUNITIES_PER_RUN,
) -> dict:
    signals_seen = 0
    signals_new = 0
    signals_duplicate = 0

    # Gate-passing candidates are collected first and only turned into
    # Opportunities after the whole batch is ranked — so input order can
    # never decide which candidates make the cap (see compute_pre_rank_score).
    ranked_pool: list[dict] = []

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
        if not candidates:
            continue

        candidate_types = {candidate["evidence_type"] for candidate in candidates}
        if not passes_candidate_gate(candidate_types):
            # e.g. product_launch_signal fired alone — context, not evidence.
            # Signal (incl. metadata_json["is_launch"]) stays saved above.
            continue

        engagement_score = normalized["metadata"].get("engagement_score")
        pre_rank_score = compute_pre_rank_score(candidates, engagement_score)
        ranked_pool.append({
            "normalized": normalized,
            "candidates": candidates,
            "pre_rank_score": pre_rank_score,
            "commerce_tier": _commerce_tier(candidate_types),
            "source_url": normalized["source_url"] or "",
        })

    # Deterministic ordering: commerce tier first (Tier 1 = genuine
    # pain/purchase/alternative-seeking evidence always outranks Tier 2 =
    # traction/launch-only, regardless of engagement magnitude — see
    # _commerce_tier), then highest pre-rank within a tier, source_url as a
    # stable final tie-breaker. This is commercial triage priority only,
    # never Opportunity Score / Evidence Confidence.
    ranked_pool.sort(key=lambda entry: (-entry["commerce_tier"], -entry["pre_rank_score"], entry["source_url"]))

    candidates_created = 0
    candidates_skipped_cap = 0

    for entry in ranked_pool:
        if candidates_created >= max_new_opportunities_per_run:
            candidates_skipped_cap += 1
            continue

        _create_opportunity_with_evidence(db, entry["normalized"], entry["candidates"])
        db.commit()
        candidates_created += 1

    return {
        "signals_seen": signals_seen,
        "signals_new": signals_new,
        "signals_duplicate": signals_duplicate,
        "candidates_created": candidates_created,
        "candidates_skipped_cap": candidates_skipped_cap,
    }
