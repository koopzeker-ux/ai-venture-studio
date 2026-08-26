import calendar
import logging
import re

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

# LEAD fix (M3.4 pre-review): Reddit's own subreddit-naming rules (3-21
# chars, letters/digits/underscore, must start with a letter or digit) --
# validating BEFORE building a URL closes a real configuration-injection
# gap found during review: without this, a REDDIT_SUBREDDITS value
# containing "?"/"#"/".." would silently reshape the request path (e.g.
# "test?x=1" turns "/new/.rss" into a bogus query string instead of the
# intended path segment -- confirmed by direct httpx.URL construction, not
# a network escape to a different host since the host is always the fixed
# template string, but a real "config can quietly change fetch behavior"
# bug), and a control character (tab/newline/NUL) raises httpx.InvalidURL,
# which -- confirmed empirically -- is a plain Exception, NOT a subclass
# of httpx.RequestError, so it was NOT caught by the except clauses below
# and would have crashed the entire collector run (HN and RSS included,
# since collect_raw_signals() runs all three collectors in one call)
# rather than safely skipping just the one bad subreddit.
_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]{2,20}$")


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
    """Split the raw config value, then keep only names matching Reddit's
    own naming rules -- an invalid entry (path traversal, query/fragment
    characters, whitespace, a full URL, control characters, ...) is
    dropped and logged rather than ever reaching URL construction."""
    raw = settings.reddit_subreddits or ""
    valid: list[str] = []
    for name in raw.split(","):
        name = name.strip()
        if not name:
            continue
        if not _SUBREDDIT_NAME_RE.match(name):
            logger.error("reddit collector: skipping invalid subreddit name in config: %r", name)
            continue
        valid.append(name)
    return valid


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
    except httpx.InvalidURL as exc:
        # Defense-in-depth: _configured_subreddits()'s validation should
        # already prevent this, but never let a malformed URL crash the
        # whole collector run (HN/RSS included) instead of safely skipping
        # this one subreddit -- see the module-level comment on
        # _SUBREDDIT_NAME_RE for how this was found.
        logger.error("reddit collector: invalid URL for r/%s: %s", subreddit, exc)
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
