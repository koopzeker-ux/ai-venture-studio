"""Resilience tests for the RSS/Atom collector.

The httpx network layer is mocked — no real HTTP request is ever made.
feedparser must only ever parse the response content httpx already
downloaded; it must never perform its own network request.
"""
import logging

import feedparser
import httpx
import pytest

from app.collectors import rss
from app.core.config import settings as global_settings

FEED_URL = "https://feed.example/one"

VALID_RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Product Hunt</title>
    <item>
      <title>Does anyone know a tool that automates onboarding?</title>
      <link>https://www.producthunt.com/posts/example</link>
      <description>does anyone know a tool that automates onboarding well</description>
      <pubDate>Mon, 01 Jan 2025 00:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>"""

VALID_ATOM_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom Feed</title>
  <entry>
    <title>So annoying that onboarding takes 10 steps</title>
    <link href="https://example.com/atom-post"/>
    <summary>so annoying that onboarding takes 10 steps to finish</summary>
    <updated>2025-01-01T00:00:00Z</updated>
  </entry>
</feed>"""

# Produces feedparser entries == [] with bozo == 1, which is the code
# path that actually logs "malformed feed" (see rss._fetch_feed).
MALFORMED_XML = b"this is not xml at all, just garbage text 12345"


class _FakeResponse:
    def __init__(self, status_code=200, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", FEED_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


@pytest.fixture(autouse=True)
def _single_feed_configured(monkeypatch):
    monkeypatch.setattr(global_settings, "rss_feed_urls", FEED_URL)


def test_fetch_recent_signals_valid_rss(monkeypatch):
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResponse(200, VALID_RSS_XML)

    monkeypatch.setattr(rss.httpx, "get", fake_get)

    signals = rss.fetch_recent_signals()

    assert calls == [FEED_URL]
    assert len(signals) == 1
    assert signals[0]["source"] == "rss"
    assert signals[0]["source_url"] == "https://www.producthunt.com/posts/example"
    assert signals[0]["title"] == "Does anyone know a tool that automates onboarding?"


def test_fetch_recent_signals_valid_atom(monkeypatch):
    monkeypatch.setattr(rss.httpx, "get", lambda url, timeout=None: _FakeResponse(200, VALID_ATOM_XML))

    signals = rss.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["source"] == "rss"
    assert signals[0]["source_url"] == "https://example.com/atom-post"
    assert signals[0]["title"] == "So annoying that onboarding takes 10 steps"


def test_fetch_recent_signals_http_4xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(rss.httpx, "get", lambda url, timeout=None: _FakeResponse(404))

    with caplog.at_level(logging.ERROR):
        signals = rss.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 404" in r.message for r in caplog.records)


def test_fetch_recent_signals_http_5xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(rss.httpx, "get", lambda url, timeout=None: _FakeResponse(503))

    with caplog.at_level(logging.ERROR):
        signals = rss.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 503" in r.message for r in caplog.records)


def test_fetch_recent_signals_timeout_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, timeout=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(rss.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = rss.fetch_recent_signals()

    assert signals == []
    assert any("timed out" in r.message for r in caplog.records)


def test_fetch_recent_signals_connect_error_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(rss.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = rss.fetch_recent_signals()

    assert signals == []
    assert any("network error" in r.message for r in caplog.records)


def test_fetch_recent_signals_malformed_xml_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(rss.httpx, "get", lambda url, timeout=None: _FakeResponse(200, MALFORMED_XML))

    with caplog.at_level(logging.ERROR):
        signals = rss.fetch_recent_signals()

    assert signals == []
    assert any("malformed feed" in r.message for r in caplog.records)


def test_httpx_downloads_and_feedparser_only_parses_response_content(monkeypatch):
    """Network boundary proof: httpx.get is the only network call, and
    feedparser.parse receives the already-downloaded bytes rather than a
    URL string it could fetch on its own."""
    download_calls = []

    def fake_get(url, timeout=None):
        download_calls.append(url)
        return _FakeResponse(200, VALID_RSS_XML)

    monkeypatch.setattr(rss.httpx, "get", fake_get)

    parse_calls = []
    real_parse = feedparser.parse

    def spy_parse(*args, **kwargs):
        parse_calls.append(args[0] if args else kwargs.get("url_file_stream_or_string"))
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(rss.feedparser, "parse", spy_parse)

    rss.fetch_recent_signals()

    assert download_calls == [FEED_URL]
    assert len(parse_calls) == 1
    assert parse_calls[0] == VALID_RSS_XML
    assert parse_calls[0] != FEED_URL
