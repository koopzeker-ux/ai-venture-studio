"""Independent REVIEWER validation of M3.1's collector-run AgentRun logging.

app.collectors.run_collectors.main() is expected to persist exactly one
AgentRun row per invocation, on both the success and failure path, using
the existing AgentRun schema (no schema change). Network calls are never
made here — collect_raw_signals() is mocked directly.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.collectors.pipeline as pipeline_module
import app.collectors.run_collectors as run_collectors
from app.db.session import Base
from app.models.entities import AgentRun


@pytest.fixture
def agentrun_session(monkeypatch):
    """Points run_collectors.SessionLocal at an isolated in-memory DB, so
    main() persists AgentRun (and any Signal/Opportunity/Evidence) rows
    there instead of touching real Postgres."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(run_collectors, "SessionLocal", Session)

    session = Session()
    yield session
    session.close()
    engine.dispose()


def _raw_signal(source, source_url):
    return {
        "source": source,
        "source_url": source_url,
        "title": "wish there was a tool for this",
        "content": "wish there was a tool for this",
        "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
    }


def _hn_like_raw_signal(source_url="https://news.ycombinator.com/item?id=1"):
    return _raw_signal("hackernews", source_url)


def test_normal_collector_run_produces_exactly_one_successful_agentrun(agentrun_session, monkeypatch, capsys):
    raw_signals = [
        _raw_signal("hackernews", "https://news.ycombinator.com/item?id=1"),
        _raw_signal("rss", "https://www.producthunt.com/posts/x"),
    ]

    monkeypatch.setattr(run_collectors, "collect_raw_signals", lambda: raw_signals)

    run_collectors.main()

    runs = agentrun_session.scalars(select(AgentRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.agent_name == "collectors"
    assert run.task_type == "market_intelligence_collector_run"
    assert run.success is True
    assert "raw_signals=2" in run.input_summary
    assert "hackernews=1" in run.input_summary
    assert "rss=1" in run.input_summary
    assert "signals_seen=2" in run.output_summary
    assert "signals_new=2" in run.output_summary
    assert "candidates_created=" in run.output_summary
    assert "candidates_skipped_cap=" in run.output_summary


def test_empty_collector_run_still_produces_exactly_one_successful_agentrun(agentrun_session, monkeypatch):
    monkeypatch.setattr(run_collectors, "collect_raw_signals", lambda: [])

    run_collectors.main()

    runs = agentrun_session.scalars(select(AgentRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.success is True
    assert "raw_signals=0" in run.input_summary
    assert "signals_seen=0" in run.output_summary
    assert "signals_new=0" in run.output_summary
    assert "candidates_created=0" in run.output_summary


def test_pipeline_failure_path_produces_exactly_one_failed_agentrun(agentrun_session, monkeypatch):
    raw_signals = [_hn_like_raw_signal()]
    monkeypatch.setattr(run_collectors, "collect_raw_signals", lambda: raw_signals)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated pipeline failure")

    # main() does a local `from app.collectors.pipeline import
    # process_raw_signals` at call time, so patching the function on its
    # defining module is what actually takes effect inside main().
    monkeypatch.setattr(pipeline_module, "process_raw_signals", _raise)

    run_collectors.main()

    runs = agentrun_session.scalars(select(AgentRun)).all()
    assert len(runs) == 1
    run = runs[0]
    assert run.success is False
    assert "raw_signals=1" in run.input_summary
    assert "pipeline processing failed" in run.output_summary
    assert "simulated pipeline failure" in run.output_summary
