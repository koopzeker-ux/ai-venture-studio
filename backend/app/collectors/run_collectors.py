import logging

from app.collectors.hackernews import fetch_recent_signals as fetch_hackernews_signals
from app.collectors.reddit import fetch_recent_signals as fetch_reddit_signals
from app.collectors.rss import fetch_recent_signals as fetch_rss_signals
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.entities import AgentRun

logger = logging.getLogger(__name__)


def collect_raw_signals() -> list[dict]:
    """Run all enabled connectors and return their combined raw signal dicts."""
    raw_signals: list[dict] = []

    if settings.hackernews_enabled:
        raw_signals.extend(fetch_hackernews_signals())

    if settings.rss_enabled:
        raw_signals.extend(fetch_rss_signals())

    if settings.reddit_enabled:
        raw_signals.extend(fetch_reddit_signals())

    return raw_signals


def _count_by_source(raw_signals: list[dict]) -> dict[str, int]:
    by_source: dict[str, int] = {}
    for signal in raw_signals:
        source = signal.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1
    return by_source


def _format_breakdown(by_source: dict[str, int]) -> str:
    return ", ".join(f"{source}={count}" for source, count in sorted(by_source.items()))


def _log_agent_run(db, raw_signals: list[dict], breakdown: str, stats: dict | None, error_detail: str | None) -> None:
    """Persist exactly one AgentRun row summarizing this collector run.

    Existing AgentRun schema has no structured JSON field, so run/counting
    information is encoded as human-readable text in input_summary /
    output_summary — no schema change, no new table.
    """
    if stats is not None:
        output_summary = (
            f"signals_seen={stats['signals_seen']}, signals_new={stats['signals_new']}, "
            f"signals_duplicate={stats['signals_duplicate']}, "
            f"candidates_created={stats['candidates_created']}, "
            f"candidates_skipped_cap={stats['candidates_skipped_cap']}"
        )
    else:
        output_summary = f"pipeline processing failed: {error_detail}"

    run = AgentRun(
        agent_name="collectors",
        task_type="market_intelligence_collector_run",
        input_summary=f"raw_signals={len(raw_signals)} ({breakdown})",
        output_summary=output_summary,
        model=None,
        cost_eur=0.0,
        success=stats is not None,
    )
    db.add(run)
    db.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    raw_signals = collect_raw_signals()
    by_source = _count_by_source(raw_signals)
    breakdown = _format_breakdown(by_source)

    from app.collectors.pipeline import process_raw_signals

    db = SessionLocal()
    stats: dict | None = None
    error_detail: str | None = None
    try:
        try:
            stats = process_raw_signals(db, raw_signals)
        except Exception as exc:
            db.rollback()
            error_detail = str(exc)
            logger.error("collectors run: pipeline processing failed: %s", exc)

        _log_agent_run(db, raw_signals, breakdown, stats, error_detail)
    finally:
        db.close()

    if stats is not None:
        print(
            f"collectors run: {len(raw_signals)} raw signals collected ({breakdown}) | "
            f"pipeline: seen={stats['signals_seen']} new={stats['signals_new']} "
            f"duplicate={stats['signals_duplicate']} candidates={stats['candidates_created']}"
        )
    else:
        print(f"collectors run: {len(raw_signals)} raw signals collected ({breakdown}) | pipeline FAILED")


if __name__ == "__main__":
    main()
