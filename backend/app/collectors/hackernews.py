import logging

import httpx

logger = logging.getLogger(__name__)

ALGOLIA_HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
DEFAULT_LIMIT = 30
REQUEST_TIMEOUT = 10.0


def fetch_recent_signals(limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Fetch recent Hacker News stories via the Algolia HN Search API.

    Never raises: any network, HTTP, or parsing failure is logged and
    results in an empty list.
    """
    params = {
        "tags": "story",
        "hitsPerPage": str(limit),
    }

    try:
        response = httpx.get(ALGOLIA_HN_SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        logger.error("hackernews collector: request timed out")
        return []
    except httpx.HTTPStatusError as exc:
        logger.error("hackernews collector: HTTP %s error", exc.response.status_code)
        return []
    except httpx.RequestError as exc:
        logger.error("hackernews collector: network error: %s", exc)
        return []
    except ValueError:
        logger.error("hackernews collector: malformed JSON response")
        return []

    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        logger.error("hackernews collector: unexpected response shape")
        return []

    signals: list[dict] = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        try:
            signal = _normalize_hit(hit)
        except (TypeError, ValueError) as exc:
            logger.error("hackernews collector: skipping malformed hit: %s", exc)
            continue
        if signal is not None:
            signals.append(signal)

    return signals


def _normalize_hit(hit: dict) -> dict | None:
    object_id = hit.get("objectID")
    title = hit.get("title")
    if not object_id or not title:
        return None

    hn_url = f"https://news.ycombinator.com/item?id={object_id}"
    source_url = hit.get("url") or hn_url
    content = hit.get("story_text") or title

    points = hit.get("points")
    engagement_score = int(points) if isinstance(points, (int, float)) else None

    created_at_i = hit.get("created_at_i")
    published_at = float(created_at_i) if isinstance(created_at_i, (int, float)) else None

    return {
        "source": "hackernews",
        "source_url": source_url,
        "title": title,
        "content": content,
        "metadata": {
            "engagement_score": engagement_score,
            "published_at": published_at,
        },
    }
