import calendar
import logging

import feedparser
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0


def fetch_recent_signals() -> list[dict]:
    """Fetch recent entries from the configured RSS/Atom feeds.

    Feed content is fetched explicitly via httpx; feedparser is used only
    to parse the already-downloaded response body, so no network access
    happens outside of the httpx calls made here.

    Never raises: any network, HTTP, or parsing failure for a given feed
    is logged and that feed is skipped, without affecting other feeds.
    """
    signals: list[dict] = []

    for feed_url in _feed_urls():
        signals.extend(_fetch_feed(feed_url))

    return signals


def _feed_urls() -> list[str]:
    raw = settings.rss_feed_urls or ""
    return [url.strip() for url in raw.split(",") if url.strip()]


def _fetch_feed(feed_url: str) -> list[dict]:
    try:
        response = httpx.get(feed_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        body = response.content
    except httpx.TimeoutException:
        logger.error("rss collector: request timed out for %s", feed_url)
        return []
    except httpx.HTTPStatusError as exc:
        logger.error("rss collector: HTTP %s error for %s", exc.response.status_code, feed_url)
        return []
    except httpx.RequestError as exc:
        logger.error("rss collector: network error for %s: %s", feed_url, exc)
        return []

    try:
        parsed = feedparser.parse(body)
    except Exception as exc:  # feedparser does not guarantee a specific exception type
        logger.error("rss collector: malformed feed content for %s: %s", feed_url, exc)
        return []

    entries = getattr(parsed, "entries", None)
    if not entries:
        if getattr(parsed, "bozo", False):
            logger.error("rss collector: malformed feed for %s", feed_url)
        return []

    signals: list[dict] = []
    for entry in entries:
        signal = _normalize_entry(entry, feed_url)
        if signal is not None:
            signals.append(signal)

    return signals


def _normalize_entry(entry, feed_url: str) -> dict | None:
    title = entry.get("title")
    if not title:
        return None

    source_url = entry.get("link") or feed_url
    content = entry.get("summary") or entry.get("description") or title

    published_at = None
    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_parsed is not None:
        try:
            published_at = float(calendar.timegm(published_parsed))
        except (TypeError, ValueError, OverflowError):
            published_at = None

    return {
        "source": "rss",
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {
            "engagement_score": None,
            "published_at": published_at,
        },
    }
