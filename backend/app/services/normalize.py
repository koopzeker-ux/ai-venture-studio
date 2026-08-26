"""Source-agnostic normalization of raw signals.

Input/output shape only — no knowledge of any specific source
(hackernews, rss, reddit, ...). See CLAUDE.md / M2.1 task for the
raw signal contract.
"""
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_NOISE_RE = re.compile(r"[*_`#>~]+")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = _MD_LINK_RE.sub(r"\1", value)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MD_NOISE_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def _clean_engagement_score(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_published_at(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_is_launch(value) -> bool:
    """Coerce any raw is_launch value to a safe bool. Defaults to False for
    missing/invalid input — never raises, never breaks the pipeline."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _clean_optional_str(value) -> str | None:
    """Coerce an optional free-text provenance field (subreddit, an
    external item id, ...) to a trimmed string, or None. Never fabricates
    a value — missing/blank/non-string input stays None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _clean_metadata(metadata: dict | None) -> dict:
    metadata = metadata or {}
    return {
        "engagement_score": _clean_engagement_score(metadata.get("engagement_score")),
        "published_at": _clean_published_at(metadata.get("published_at")),
        "is_launch": _clean_is_launch(metadata.get("is_launch")),
        # M3.4: optional source-agnostic provenance fields. Any source may
        # populate these (currently only app.collectors.reddit does);
        # sources that don't stay None -- never fabricated.
        "subreddit": _clean_optional_str(metadata.get("subreddit")),
        "external_id": _clean_optional_str(metadata.get("external_id")),
    }


def normalize_raw_signal(raw: dict) -> dict:
    """Normalize a source-agnostic raw signal dict into a clean, generic shape.

    Expects: {source, source_url, title, content,
              metadata: {engagement_score, published_at, is_launch,
                         subreddit, external_id}}
    subreddit/external_id are optional provenance fields (M3.4); sources
    that don't supply them get None, never a fabricated value.
    """
    return {
        "source": (raw.get("source") or "").strip(),
        "source_url": (raw.get("source_url") or "").strip(),
        "title": _clean_text(raw.get("title")),
        "content": _clean_text(raw.get("content")),
        "metadata": _clean_metadata(raw.get("metadata")),
    }
