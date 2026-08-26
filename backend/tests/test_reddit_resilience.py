"""Resilience tests for the Reddit collector (M3.4).

The httpx network layer is mocked — no real HTTP request is ever made and
no real Reddit call is permitted in this test suite (BUILDER may only use
mocked/local fixture feeds; LEAD performs the bounded live dogfood later).
feedparser must only ever parse the response body httpx already
downloaded; it must never perform its own network request.
"""
import logging

import feedparser
import httpx
import pytest

from app.collectors import reddit
from app.core.config import settings as global_settings

VALID_ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>r/smallbusiness</title>
  <entry>
    <title>We spend 10 hours a week doing invoicing by hand</title>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/smallbusiness/comments/abc123/we_spend_10_hours/"/>
    <content type="html">We spend 10 hours a week doing invoicing by hand, is there a tool that automates this?</content>
    <updated>2025-01-01T00:00:00Z</updated>
  </entry>
</feed>"""

# Reddit's real Atom feed exposes the post body in <content>, not
# <summary>/<description> -- this fixture has neither, only title + content,
# to prove the collector actually reads it and doesn't fall back to
# title-only when meaningful body text is available.
ATOM_XML_NO_SUMMARY_TAG = VALID_ATOM_XML

MALFORMED_XML = b"this is not xml at all, just garbage text 12345"

MISSING_TITLE_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>t3_notitle</id>
    <link href="https://www.reddit.com/r/smallbusiness/comments/notitle/"/>
    <content type="html">no title on this entry</content>
  </entry>
</feed>"""

MISSING_DATE_AND_ID_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>No date or id on this one</title>
    <link href="https://www.reddit.com/r/smallbusiness/comments/nodate/"/>
    <content type="html">no updated/published tag, no id tag</content>
  </entry>
</feed>"""

MISSING_LINK_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>No link on this entry</title>
    <content type="html">no link or id tag at all</content>
  </entry>
</feed>"""


class _FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://www.reddit.com/r/x/new/.rss")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


@pytest.fixture(autouse=True)
def _configured_subreddits(monkeypatch):
    monkeypatch.setattr(global_settings, "reddit_subreddits", "smallbusiness")


def test_fetch_recent_signals_valid_reddit_atom(monkeypatch):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(200, VALID_ATOM_XML)

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    signals = reddit.fetch_recent_signals()

    assert calls == ["https://www.reddit.com/r/smallbusiness/new/.rss"]
    assert len(signals) == 1
    s = signals[0]
    assert s["source"] == "reddit"
    assert s["source_url"] == "https://www.reddit.com/r/smallbusiness/comments/abc123/we_spend_10_hours/"
    assert s["title"] == "We spend 10 hours a week doing invoicing by hand"
    assert "is there a tool that automates this" in s["content"]
    assert s["metadata"]["subreddit"] == "smallbusiness"
    assert s["metadata"]["external_id"] == "t3_abc123"
    # Reddit's public RSS never exposes vote/comment counts -- must stay
    # None, never fabricated as 0 (see app.collectors.reddit docstring).
    assert s["metadata"]["engagement_score"] is None
    assert s["metadata"]["is_launch"] is False
    assert s["metadata"]["published_at"] is not None


def test_fetch_recent_signals_reads_body_from_content_tag_not_just_title(monkeypatch):
    """Reddit's Atom feed puts post body in <content>, not <summary> --
    the collector must not silently fall back to title-only when real
    body text is legitimately available."""
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(200, ATOM_XML_NO_SUMMARY_TAG))

    signals = reddit.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["content"] != signals[0]["title"]
    assert "automates this" in signals[0]["content"]


def test_fetch_recent_signals_multiple_configured_subreddits(monkeypatch):
    monkeypatch.setattr(global_settings, "reddit_subreddits", "smallbusiness, SaaS ,  Entrepreneur")
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(200, VALID_ATOM_XML)

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    signals = reddit.fetch_recent_signals()

    assert calls == [
        "https://www.reddit.com/r/smallbusiness/new/.rss",
        "https://www.reddit.com/r/SaaS/new/.rss",
        "https://www.reddit.com/r/Entrepreneur/new/.rss",
    ]
    assert len(signals) == 3


def test_fetch_recent_signals_no_subreddits_configured_makes_no_requests(monkeypatch):
    monkeypatch.setattr(global_settings, "reddit_subreddits", "")
    calls = []
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: calls.append(url))

    signals = reddit.fetch_recent_signals()

    assert signals == []
    assert calls == []


def test_fetch_recent_signals_http_4xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(404))

    with caplog.at_level(logging.ERROR):
        signals = reddit.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 404" in r.message for r in caplog.records)


def test_fetch_recent_signals_http_5xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(503))

    with caplog.at_level(logging.ERROR):
        signals = reddit.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 503" in r.message for r in caplog.records)


def test_fetch_recent_signals_timeout_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, timeout=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = reddit.fetch_recent_signals()

    assert signals == []
    assert any("timed out" in r.message for r in caplog.records)


def test_fetch_recent_signals_connect_error_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = reddit.fetch_recent_signals()

    assert signals == []
    assert any("network error" in r.message for r in caplog.records)


def test_fetch_recent_signals_malformed_xml_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(200, MALFORMED_XML))

    with caplog.at_level(logging.ERROR):
        signals = reddit.fetch_recent_signals()

    assert signals == []
    assert any("malformed feed" in r.message for r in caplog.records)


def test_fetch_recent_signals_missing_title_entry_is_skipped(monkeypatch):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(200, MISSING_TITLE_XML))

    signals = reddit.fetch_recent_signals()

    assert signals == []


def test_fetch_recent_signals_missing_date_and_id_does_not_crash_or_fabricate(monkeypatch):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(200, MISSING_DATE_AND_ID_XML))

    signals = reddit.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["metadata"]["published_at"] is None
    assert signals[0]["metadata"]["external_id"] is None


def test_fetch_recent_signals_missing_link_falls_back_to_feed_url(monkeypatch):
    monkeypatch.setattr(reddit.httpx, "get", lambda url, timeout=None: _FakeResponse(200, MISSING_LINK_XML))

    signals = reddit.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["source_url"] == "https://www.reddit.com/r/smallbusiness/new/.rss"


def test_one_subreddit_failure_does_not_affect_another(monkeypatch):
    monkeypatch.setattr(global_settings, "reddit_subreddits", "broken, smallbusiness")

    def fake_get(url, timeout=None):
        if "r/broken" in url:
            raise httpx.ConnectError("connection refused")
        return _FakeResponse(200, VALID_ATOM_XML)

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    signals = reddit.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["metadata"]["subreddit"] == "smallbusiness"


def test_no_auth_headers_or_credentials_are_sent(monkeypatch):
    """Public compliant access only -- no OAuth, no login cookies, no
    access tokens (CLAUDE.md / M3.4 task §2/§13)."""
    captured_kwargs = {}

    def fake_get(url, timeout=None, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeResponse(200, VALID_ATOM_XML)

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    reddit.fetch_recent_signals()

    assert captured_kwargs == {}


def test_httpx_downloads_and_feedparser_only_parses_response_content(monkeypatch):
    """Network boundary proof: httpx.get is the only network call, and
    feedparser.parse receives the already-downloaded bytes rather than a
    URL string it could fetch on its own -- no real Reddit request is ever
    made anywhere in this collector."""
    download_calls = []

    def fake_get(url, timeout=None):
        download_calls.append(url)
        return _FakeResponse(200, VALID_ATOM_XML)

    monkeypatch.setattr(reddit.httpx, "get", fake_get)

    parse_calls = []
    real_parse = feedparser.parse

    def spy_parse(*args, **kwargs):
        parse_calls.append(args[0] if args else kwargs.get("url_file_stream_or_string"))
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(reddit.feedparser, "parse", spy_parse)

    reddit.fetch_recent_signals()

    assert download_calls == ["https://www.reddit.com/r/smallbusiness/new/.rss"]
    assert len(parse_calls) == 1
    assert parse_calls[0] == VALID_ATOM_XML
    assert parse_calls[0] != "https://www.reddit.com/r/smallbusiness/new/.rss"
