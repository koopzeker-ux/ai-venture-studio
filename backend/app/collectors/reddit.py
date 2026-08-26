import calendar
import logging

import feedparser
import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10.0

# Compliant public access method only (CLAUDE.md / M3.4 task): each
# subreddit's own public RSS feed, no OAuth, no scraping, no browser
# automation, no login. Sorted "new" so discovery sees fresh posts rather
# than whatever Reddit's own hot-ranking already surfaced.
REDDIT_RSS_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/new/.rss"


def fetch_recent_signals() -> list[dict]:
    """Fetch recent posts from the explicitly configured subreddit list
    (settings.reddit_subreddits) via each subreddit's public RSS feed.

    Feed content is fetched explicitly via httpx; feedparser is used only
    to parse the already-downloaded response body, so no network access
    happens outside of the httpx calls made here.

    Never raises: any network, HTTP, or parsing failure for a given
    subreddit is logged and that subreddit is skipped, without affecting
    the others.
    """
    signals: list[dict] = []

    for subreddit in _configured_subreddits():
        signals.extend(_fetch_subreddit(subreddit))

    return signals


def _configured_subreddits() -> list[str]:
    raw = settings.reddit_subreddits or ""
    return [name.strip() for name in raw.split(",") if name.strip()]


def _fetch_subreddit(subreddit: str) -> list[dict]:
    feed_url = REDDIT_RSS_URL_TEMPLATE.format(subreddit=subreddit)
    try:
        response = httpx.get(feed_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        body = response.content
    except httpx.TimeoutException:
        logger.error("reddit collector: request timed out for r/%s", subreddit)
        return []
    except httpx.HTTPStatusError as exc:
        logger.error("reddit collector: HTTP %s error for r/%s", exc.response.status_code, subreddit)
        return []
    except httpx.RequestError as exc:
        logger.error("reddit collector: network error for r/%s: %s", subreddit, exc)
        return []

    try:
        parsed = feedparser.parse(body)
    except Exception as exc:  # feedparser does not guarantee a specific exception type
        logger.error("reddit collector: malformed feed content for r/%s: %s", subreddit, exc)
        return []

    entries = getattr(parsed, "entries", None)
    if not entries:
        if getattr(parsed, "bozo", False):
            logger.error("reddit collector: malformed feed for r/%s", subreddit)
        return []

    signals: list[dict] = []
    for entry in entries:
        signal = _normalize_entry(entry, subreddit, feed_url)
        if signal is not None:
            signals.append(signal)

    return signals


def _entry_body_text(entry) -> str | None:
    """Reddit's Atom feed puts the post body/preview HTML in <content>, not
    <summary>/<description> -- check both, title-only as a last resort.
    Same defensive fallback style as app.collectors.rss._normalize_entry.
    """
    body = entry.get("summary") or entry.get("description")
    if body:
        return body

    content_list = entry.get("content")
    if content_list:
        first = content_list[0]
        if isinstance(first, dict):
            return first.get("value")

    return None


def _normalize_entry(entry, subreddit: str, feed_url: str) -> dict | None:
    title = entry.get("title")
    if not title:
        return None

    source_url = entry.get("link") or feed_url
    content = _entry_body_text(entry) or title

    published_at = None
    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if published_parsed is not None:
        try:
            published_at = float(calendar.timegm(published_parsed))
        except (TypeError, ValueError, OverflowError):
            published_at = None

    # entry.get("id"): Reddit's Atom <id> tag (stable per post). Missing
    # stays missing -- never fabricated.
    external_id = entry.get("id") or None

    return {
        "source": "reddit",
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {
            # Reddit's public RSS/Atom feed does not expose vote/comment
            # counts. Stays None (never fabricated as 0) -- traction_signal
            # can therefore never fire for a Reddit item; only genuine
            # commercial-intent triggers can promote one (see M3.4 task
            # §7/§9: commerce-first, no fake engagement/economics).
            "engagement_score": None,
            "published_at": published_at,
            "is_launch": False,
            "subreddit": subreddit,
            "external_id": external_id,
        },
    }
