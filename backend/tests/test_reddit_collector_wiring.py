"""M3.4: Reddit collector wiring into app.collectors.run_collectors.

collect_raw_signals() must only ever call the Reddit collector when
settings.reddit_enabled is True -- ships disabled by default (see
app.core.config), so no live Reddit traffic happens unless LEAD
explicitly turns it on. No network call anywhere in this file.
"""
import app.collectors.run_collectors as run_collectors
from app.core.config import settings


def test_reddit_collector_not_called_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "hackernews_enabled", False)
    monkeypatch.setattr(settings, "rss_enabled", False)
    monkeypatch.setattr(settings, "reddit_enabled", False)

    calls = []
    monkeypatch.setattr(run_collectors, "fetch_reddit_signals", lambda: calls.append("reddit") or [])

    raw_signals = run_collectors.collect_raw_signals()

    assert calls == []
    assert raw_signals == []


def test_reddit_collector_called_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "hackernews_enabled", False)
    monkeypatch.setattr(settings, "rss_enabled", False)
    monkeypatch.setattr(settings, "reddit_enabled", True)

    reddit_signal = {
        "source": "reddit",
        "source_url": "https://www.reddit.com/r/smallbusiness/comments/x/",
        "title": "t",
        "content": "c",
        "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
    }
    monkeypatch.setattr(run_collectors, "fetch_reddit_signals", lambda: [reddit_signal])

    raw_signals = run_collectors.collect_raw_signals()

    assert raw_signals == [reddit_signal]


def test_reddit_disabled_by_default():
    """Ships inert -- BUILDER must never enable live Reddit discovery."""
    assert settings.reddit_enabled is False
