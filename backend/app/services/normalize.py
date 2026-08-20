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


def _clean_metadata(metadata: dict | None) -> dict:
    metadata = metadata or {}
    return {
        "engagement_score": _clean_engagement_score(metadata.get("engagement_score")),
        "published_at": _clean_published_at(metadata.get("published_at")),
    }


def normalize_raw_signal(raw: dict) -> dict:
    """Normalize a source-agnostic raw signal dict into a clean, generic shape.

    Expects: {source, source_url, title, content, metadata: {engagement_score, published_at}}
    """
    return {
        "source": (raw.get("source") or "").strip(),
        "source_url": (raw.get("source_url") or "").strip(),
        "title": _clean_text(raw.get("title")),
        "content": _clean_text(raw.get("content")),
        "metadata": _clean_metadata(raw.get("metadata")),
    }
