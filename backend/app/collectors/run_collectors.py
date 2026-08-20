import logging

from app.collectors.hackernews import fetch_recent_signals as fetch_hackernews_signals
from app.collectors.rss import fetch_recent_signals as fetch_rss_signals
from app.core.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)


def collect_raw_signals() -> list[dict]:
    """Run all enabled connectors and return their combined raw signal dicts."""
    raw_signals: list[dict] = []

    if settings.hackernews_enabled:
        raw_signals.extend(fetch_hackernews_signals())

    if settings.rss_enabled:
        raw_signals.extend(fetch_rss_signals())

    return raw_signals


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    raw_signals = collect_raw_signals()

    from app.collectors.pipeline import process_raw_signals

    db = SessionLocal()
    try:
        process_raw_signals(db, raw_signals)
    finally:
        db.close()

    by_source: dict[str, int] = {}
    for signal in raw_signals:
        source = signal.get("source", "unknown")
        by_source[source] = by_source.get(source, 0) + 1

    breakdown = ", ".join(f"{source}={count}" for source, count in sorted(by_source.items()))
    print(f"collectors run: {len(raw_signals)} raw signals collected ({breakdown})")


if __name__ == "__main__":
    main()
