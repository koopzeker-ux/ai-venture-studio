import logging

import httpx

logger = logging.getLogger(__name__)

ALGOLIA_SEARCH_BY_DATE_URL = "https://hn.algolia.com/api/v1/search_by_date"
ALGOLIA_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
DEFAULT_LIMIT = 30
TRACTION_MIN_POINTS = 50
REQUEST_TIMEOUT = 10.0

_LAUNCH_TITLE_PREFIXES = ("show hn:", "launch hn:")


def fetch_recent_signals(limit: int = DEFAULT_LIMIT) -> list[dict]:
    """Fetch Hacker News stories via two independent Algolia queries.

    Query A (recency): search_by_date, recent stories.
    Query B (traction): search, stories with points > TRACTION_MIN_POINTS.

    Each query is fetched and error-handled independently — a failure in
    one never prevents the other from returning results. Results are
    combined and deduplicated on Hacker News `objectID`.

    Never raises: any network, HTTP, or parsing failure is logged and
    that query simply contributes no hits.
    """
    recency_hits = _fetch_recency_hits(limit)
    traction_hits = _fetch_traction_hits(limit)

    signals: list[dict] = []
    seen_object_ids: set[str] = set()
    for hit in recency_hits + traction_hits:
        if not isinstance(hit, dict):
            continue
        object_id = hit.get("objectID")
        if not object_id or object_id in seen_object_ids:
            continue
        try:
            signal = _normalize_hit(hit)
        except (TypeError, ValueError) as exc:
            logger.error("hackernews collector: skipping malformed hit: %s", exc)
            continue
        if signal is None:
            continue
        seen_object_ids.add(object_id)
        signals.append(signal)

    return signals


def _fetch_recency_hits(limit: int) -> list[dict]:
    params = {
        "tags": "story",
        "hitsPerPage": str(limit),
    }
    return _run_algolia_query(ALGOLIA_SEARCH_BY_DATE_URL, params, "recency")


def _fetch_traction_hits(limit: int) -> list[dict]:
    params = {
        "tags": "story",
        "numericFilters": f"points>{TRACTION_MIN_POINTS}",
        "hitsPerPage": str(limit),
    }
    return _run_algolia_query(ALGOLIA_SEARCH_URL, params, "traction")


def _run_algolia_query(url: str, params: dict, query_label: str) -> list[dict]:
    try:
        response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
    except httpx.TimeoutException:
        logger.error("hackernews collector (%s): request timed out", query_label)
        return []
    except httpx.HTTPStatusError as exc:
        logger.error("hackernews collector (%s): HTTP %s error", query_label, exc.response.status_code)
        return []
    except httpx.RequestError as exc:
        logger.error("hackernews collector (%s): network error: %s", query_label, exc)
        return []
    except ValueError:
        logger.error("hackernews collector (%s): malformed JSON response", query_label)
        return []

    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        logger.error("hackernews collector (%s): unexpected response shape", query_label)
        return []

    return hits


def _is_launch_title(title: str) -> bool:
    normalized = title.strip().casefold()
    return normalized.startswith(_LAUNCH_TITLE_PREFIXES)


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
            "is_launch": _is_launch_title(title),
        },
    }
