"""Resilience tests for the Hacker News collector.

All network calls are mocked — no real HTTP request is ever made. Every
failure scenario must: not raise, log the failure, and return a safe
empty list.
"""
import logging

import httpx

from app.collectors import hackernews


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, json_raises=False):
        self.status_code = status_code
        self._json_data = json_data
        self._json_raises = json_raises

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", hackernews.ALGOLIA_SEARCH_BY_DATE_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        if self._json_raises:
            raise ValueError("malformed JSON")
        return self._json_data


def test_fetch_recent_signals_success(monkeypatch):
    hits = [
        {
            "objectID": "123",
            "title": "Wish there was a tool for X",
            "url": "https://example.com/x",
            "points": 150,
            "created_at_i": 1735689600,
        },
        {
            "objectID": "456",
            "title": "",  # falsy title -> must be skipped, not crash
        },
    ]

    def fake_get(url, params=None, timeout=None):
        # M2.2: fetch_recent_signals() now issues two independent queries
        # (recency + traction); both are exercised here and return the
        # same hits, so the connector's own objectID dedup collapses them
        # back to one signal — same assertion as before the split.
        assert url in (hackernews.ALGOLIA_SEARCH_BY_DATE_URL, hackernews.ALGOLIA_SEARCH_URL)
        return _FakeResponse(200, {"hits": hits})

    monkeypatch.setattr(hackernews.httpx, "get", fake_get)

    signals = hackernews.fetch_recent_signals()

    assert len(signals) == 1
    assert signals[0]["source"] == "hackernews"
    assert signals[0]["source_url"] == "https://example.com/x"
    assert signals[0]["metadata"]["engagement_score"] == 150


def test_fetch_recent_signals_http_4xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(hackernews.httpx, "get", lambda url, params=None, timeout=None: _FakeResponse(404))

    with caplog.at_level(logging.ERROR):
        signals = hackernews.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 404" in r.message for r in caplog.records)


def test_fetch_recent_signals_http_5xx_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(hackernews.httpx, "get", lambda url, params=None, timeout=None: _FakeResponse(503))

    with caplog.at_level(logging.ERROR):
        signals = hackernews.fetch_recent_signals()

    assert signals == []
    assert any("HTTP 503" in r.message for r in caplog.records)


def test_fetch_recent_signals_timeout_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, params=None, timeout=None):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(hackernews.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = hackernews.fetch_recent_signals()

    assert signals == []
    assert any("timed out" in r.message for r in caplog.records)


def test_fetch_recent_signals_connect_error_returns_empty_list_and_logs(monkeypatch, caplog):
    def fake_get(url, params=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(hackernews.httpx, "get", fake_get)

    with caplog.at_level(logging.ERROR):
        signals = hackernews.fetch_recent_signals()

    assert signals == []
    assert any("network error" in r.message for r in caplog.records)


def test_fetch_recent_signals_malformed_json_returns_empty_list_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        hackernews.httpx, "get", lambda url, params=None, timeout=None: _FakeResponse(200, json_raises=True)
    )

    with caplog.at_level(logging.ERROR):
        signals = hackernews.fetch_recent_signals()

    assert signals == []
    assert any("malformed JSON" in r.message for r in caplog.records)
