"""M3.2 Researcher tests (app.research.run_researcher).

subprocess.run standing in for the real `claude` binary is ALWAYS mocked
here -- no real Claude call, no paid model call, no live web search. This
is the smallest vertical slice: exactly one Opportunity, exactly one model
call, no batch/scheduler/rerun.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SASession, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.entities import AgentRun, CostEvent, Evidence, Opportunity
from app.orchestration.claude_code_adapter import WorkerResult
from app.research.run_researcher import (
    DEFAULT_RESEARCH_TOOLS,
    OpportunityNotFoundError,
    ResearchAlreadyExistsError,
    ResearchPayloadError,
    _build_research_prompt,
    _compute_independently_confirmed,
    _resolve_duplicate_refs,
    _validate_research_tools,
    build_research_argv,
    dispatch_research,
    parse_research_payload,
    run_researcher,
)


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    yield session
    session.close()
    engine.dispose()


def _make_opportunity(db_session, **overrides) -> Opportunity:
    defaults = dict(
        slug="test-opp",
        title="Test Opportunity",
        thesis="Founders struggle to track invoices across tools.",
    )
    defaults.update(overrides)
    opp = Opportunity(**defaults)
    db_session.add(opp)
    db_session.commit()
    return opp


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(evidence, research_summary, **overrides):
    result_payload = {"evidence": evidence, "research_summary": research_summary}
    envelope = {
        "is_error": False,
        "session_id": "sess-research-1",
        "total_cost_usd": 0.042,
        "usage": {"input_tokens": 500, "output_tokens": 300},
        "result": json.dumps(result_payload),
    }
    envelope.update(overrides)
    return json.dumps(envelope)


def _ok_worker_result(evidence=None, research_summary="Summary text.", **overrides) -> WorkerResult:
    if evidence is None:
        evidence = [
            {"id": "e1", "claim": "Demand is growing", "claim_type": "FACT", "stance": "SUPPORTS",
             "source": "TechCrunch", "source_url": "https://example.com/a", "source_reliability": "HIGH",
             "confidence": 0.8},
        ]
    result_text = json.dumps({"evidence": evidence, "research_summary": research_summary})
    defaults = dict(
        ok=True, exit_code=0, session_id="sess-1", result_text=result_text,
        usage={"input_tokens": 10, "output_tokens": 20}, total_cost_usd=0.05,
        is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    defaults.update(overrides)
    return WorkerResult(**defaults)


def _fail_worker_result(error_kind, error_detail="researcher failed") -> WorkerResult:
    return WorkerResult(
        ok=False, exit_code=1 if error_kind == "nonzero_exit" else None, session_id=None,
        result_text=None, usage={}, total_cost_usd=None, is_error=True,
        error_kind=error_kind, error_detail=error_detail, stderr_excerpt=None,
    )


# ---------------------------------------------------------------------------
# 1-7: research argv shape / tool safety
# ---------------------------------------------------------------------------

def test_1_build_research_argv_exact_shape():
    argv = build_research_argv(prompt="research this opportunity")
    assert argv == [
        "claude",
        "-p", "research this opportunity",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--allowedTools", "WebSearch", "WebFetch",
        "--safe-mode",
    ]


def test_2_safe_mode_always_present():
    argv = build_research_argv(prompt="x")
    assert "--safe-mode" in argv
    assert argv[-1] == "--safe-mode"


def test_3_dontask_always_present():
    argv = build_research_argv(prompt="x")
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_4_default_tools_are_only_websearch_webfetch():
    assert DEFAULT_RESEARCH_TOOLS == ("WebSearch", "WebFetch")
    argv = build_research_argv(prompt="x")
    tools_start = argv.index("--allowedTools") + 1
    tools_end = argv.index("--safe-mode")
    assert argv[tools_start:tools_end] == ["WebSearch", "WebFetch"]


@pytest.mark.parametrize("forbidden_tool", ["Edit", "Write", "Bash", "Bash(pytest *)", "Bash(git *)"])
def test_5_edit_write_bash_git_all_rejected(forbidden_tool):
    with pytest.raises(ValueError):
        build_research_argv(prompt="x", allowed_tools=("WebSearch", forbidden_tool))


def test_6_no_bypass_permissions_possible():
    argv = build_research_argv(prompt="x")
    joined = " ".join(argv)
    assert "bypassPermissions" not in joined
    assert "--dangerously-skip-permissions" not in argv


def test_6b_no_continue_or_resume_flags():
    argv = build_research_argv(prompt="x")
    assert "--continue" not in argv and "-c" not in argv
    assert "--resume" not in argv and "-r" not in argv


def test_7_no_bare_flag():
    argv = build_research_argv(prompt="x")
    assert "--bare" not in argv


def test_read_tool_is_permitted_but_not_default():
    """Read is explicitly allowed by the M3.2 brief ("eventueel Read alleen
    als objectief nodig") but is not part of the default set -- this vertical
    slice found no objective need for it."""
    argv = build_research_argv(prompt="x", allowed_tools=("WebSearch", "WebFetch", "Read"))
    tools_start = argv.index("--allowedTools") + 1
    tools_end = argv.index("--safe-mode")
    assert argv[tools_start:tools_end] == ["WebSearch", "WebFetch", "Read"]
    assert "Read" not in DEFAULT_RESEARCH_TOOLS


def test_prompt_cannot_look_like_a_flag():
    with pytest.raises(ValueError):
        build_research_argv(prompt="--dangerously-skip-permissions")


def test_empty_tools_rejected():
    with pytest.raises(ValueError):
        _validate_research_tools(())


# ---------------------------------------------------------------------------
# 8-10: run_researcher outcome handling
# ---------------------------------------------------------------------------

def test_8_successful_json_parse():
    payload = _envelope(
        [{"id": "e1", "claim": "x", "source": "y"}], "Summary with all sections.",
    )
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout=payload)):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.ok is True
    assert result.session_id == "sess-research-1"
    assert result.total_cost_usd == 0.042


def test_9_malformed_json_is_structured_failure():
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout="not json {{{")):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "invalid_json"


def test_10_timeout_is_structured_failure():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1200)):
        result = run_researcher(prompt="x", repo_path="/repo", timeout_seconds=1200)
    assert result.ok is False
    assert result.error_kind == "timeout"
    assert result.exit_code is None


def test_10b_model_error_envelope_is_structured_failure():
    payload = json.dumps({"is_error": True, "result": "auth failed"})
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout=payload)):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "nonzero_exit"


def test_10c_spawn_failure_is_structured_failure():
    with patch("subprocess.run", side_effect=OSError("claude: not found")):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "spawn_error"


# ---------------------------------------------------------------------------
# 11-16: evidence contract validation
# ---------------------------------------------------------------------------

def test_11_claim_type_validation_accepts_known_rejects_unknown_value():
    payload = json.dumps({
        "evidence": [
            {"claim": "a", "source": "s", "claim_type": "FACT"},
            {"claim": "b", "source": "s", "claim_type": "TOTALLY_MADE_UP"},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].claim_type == "FACT"
    assert result.entries[1].claim_type is None
    assert any("invalid claim_type" in a for a in result.anomalies)


def test_12_stance_validation_accepts_known_rejects_unknown_value():
    payload = json.dumps({
        "evidence": [
            {"claim": "a", "source": "s", "stance": "SUPPORTS"},
            {"claim": "b", "source": "s", "stance": "CONTRADICTS"},
            {"claim": "c", "source": "s", "stance": "MAYBE"},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].stance == "SUPPORTS"
    assert result.entries[1].stance == "CONTRADICTS"
    assert result.entries[2].stance is None
    assert any("invalid stance" in a for a in result.anomalies)


def test_13_source_reliability_validation_accepts_known_rejects_unknown_value():
    payload = json.dumps({
        "evidence": [
            {"claim": "a", "source": "s", "source_reliability": "HIGH"},
            {"claim": "b", "source": "s", "source_reliability": "MADE_UP"},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].source_reliability == "HIGH"
    assert result.entries[1].source_reliability == "UNKNOWN"
    assert any("invalid source_reliability" in a for a in result.anomalies)


def test_14_unknown_is_a_legitimate_value_not_an_anomaly():
    payload = json.dumps({
        "evidence": [
            {"claim": "a", "source": "s", "claim_type": "UNKNOWN", "source_reliability": "UNKNOWN"},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].claim_type == "UNKNOWN"
    assert result.entries[0].source_reliability == "UNKNOWN"
    assert result.anomalies == []


def test_15_missing_source_url_stays_null_never_fabricated():
    payload = json.dumps({"evidence": [{"claim": "a", "source": "s"}], "research_summary": "s"})
    result = parse_research_payload(payload)
    assert result.entries[0].source_url is None


def test_16_supporting_and_contradicting_evidence_both_parsed():
    payload = json.dumps({
        "evidence": [
            {"claim": "Market is underserved", "source": "a", "stance": "SUPPORTS"},
            {"claim": "Market is underserved", "source": "b", "stance": "CONTRADICTS"},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].stance == "SUPPORTS"
    assert result.entries[1].stance == "CONTRADICTS"


def test_confidence_out_of_range_falls_back_to_default_not_fabricated_precision():
    payload = json.dumps({
        "evidence": [
            {"claim": "a", "source": "s", "confidence": 5},
            {"claim": "b", "source": "s", "confidence": None},
            {"claim": "c", "source": "s", "confidence": 0.73},
        ],
        "research_summary": "s",
    })
    result = parse_research_payload(payload)
    assert result.entries[0].confidence == 0.5
    assert result.entries[1].confidence == 0.5
    assert result.entries[2].confidence == 0.73


def test_malformed_top_level_payload_raises():
    with pytest.raises(ResearchPayloadError):
        parse_research_payload("not json at all")
    with pytest.raises(ResearchPayloadError):
        parse_research_payload(json.dumps({"evidence": "not a list", "research_summary": "s"}))
    with pytest.raises(ResearchPayloadError):
        parse_research_payload(json.dumps({"evidence": [], "research_summary": ""}))


def test_json_wrapped_in_markdown_fence_still_parses():
    inner = json.dumps({"evidence": [{"claim": "a", "source": "s"}], "research_summary": "s"})
    fenced = f"```json\n{inner}\n```"
    result = parse_research_payload(fenced)
    assert len(result.entries) == 1


# ---------------------------------------------------------------------------
# 21: research_summary must require the red-team section (structural, on the prompt)
# ---------------------------------------------------------------------------

def test_21_prompt_requires_red_team_section_and_no_go_no_go_decision():
    opp = Opportunity(slug="x", title="T", thesis="Thesis text")
    prompt = _build_research_prompt(opp)
    assert "Why we might NOT" in prompt
    assert "Counter-evidence" in prompt
    assert "Key unknowns" in prompt
    assert "Supporting evidence" in prompt
    assert "Competition" in prompt
    assert "market gap" in prompt
    assert "human makes that call" in prompt
    assert "TEST/WATCH/REJECT" in prompt


def test_prompt_forbids_fabrication_explicitly():
    opp = Opportunity(slug="x", title="T", thesis="Thesis text")
    prompt = _build_research_prompt(opp)
    assert "never invent a source" in prompt
    assert "Actively search for counter-evidence" in prompt
    assert "NOT independent evidence" in prompt
    assert "untrusted data" in prompt  # web content prompt-injection guard


def test_prompt_stance_is_relative_to_claim_not_thesis():
    opp = Opportunity(slug="x", title="T", thesis="Thesis text")
    prompt = _build_research_prompt(opp)
    assert "never relative to the opportunity thesis" in prompt


# ---------------------------------------------------------------------------
# Duplicate / independently_confirmed logic (unit level)
# ---------------------------------------------------------------------------

def test_17_duplicate_temp_id_resolves_end_to_end_to_real_fk(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "Demand is growing", "source": "a", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "Demand is growing", "source": "b", "stance": "SUPPORTS", "duplicate_of": "e1"},
    ]
    worker_result = _ok_worker_result(evidence=evidence, research_summary="Summary.")

    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id).order_by(Evidence.id)).all()
    assert len(rows) == 2
    original, duplicate = rows
    assert original.duplicate_of_evidence_id is None
    assert duplicate.duplicate_of_evidence_id == original.id


def test_18_unknown_duplicate_ref_fails_safe_to_null(db_session):
    opp = _make_opportunity(db_session)
    evidence = [{"id": "e1", "claim": "a", "source": "s", "duplicate_of": "does-not-exist"}]
    worker_result = _ok_worker_result(evidence=evidence, research_summary="Summary.")

    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
    assert len(rows) == 1
    assert rows[0].duplicate_of_evidence_id is None
    assert run.success is True  # an unresolvable ref is an anomaly, not a crash
    assert "duplicate_of references unknown" in run.output_summary


def test_19_independently_confirmed_never_true_for_a_duplicate_row(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "Demand is growing", "source": "a", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "Demand is growing", "source": "b", "stance": "SUPPORTS", "duplicate_of": "e1"},
    ]
    worker_result = _ok_worker_result(evidence=evidence, research_summary="Summary.")
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id).order_by(Evidence.id)).all()
    original, duplicate = rows
    # Only one genuinely independent (non-duplicate) source exists for this
    # claim -- the duplicate never counts, and one row alone isn't "confirmed".
    assert original.independently_confirmed is False
    assert duplicate.independently_confirmed is False


def test_independently_confirmed_true_for_two_genuinely_independent_same_stance_sources(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "Demand is growing", "source": "a", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "Demand is growing", "source": "b", "stance": "SUPPORTS"},
    ]
    worker_result = _ok_worker_result(evidence=evidence, research_summary="Summary.")
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
    assert all(r.independently_confirmed for r in rows)


def test_independently_confirmed_not_true_across_disagreeing_stances_on_same_claim():
    """A CONTRADICTS row must not "confirm" a SUPPORTS row on the same claim text."""
    entries_payload = json.dumps({
        "evidence": [
            {"id": "e1", "claim": "Market is underserved", "source": "a", "stance": "SUPPORTS"},
            {"id": "e2", "claim": "Market is underserved", "source": "b", "stance": "CONTRADICTS"},
        ],
        "research_summary": "s",
    })
    payload = parse_research_payload(entries_payload)
    dup_idx = _resolve_duplicate_refs(payload.entries)
    confirmed = _compute_independently_confirmed(payload.entries, dup_idx)
    assert confirmed == [False, False]


# ---------------------------------------------------------------------------
# 20: transaction atomicity
# ---------------------------------------------------------------------------

def test_20_persistence_error_leaves_no_half_written_dossier(db_session):
    opp = _make_opportunity(db_session)
    evidence = [{"id": "e1", "claim": "a", "source": "s"}]
    worker_result = _ok_worker_result(evidence=evidence, research_summary="Summary.")

    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:  # the first commit is the Evidence+summary transaction
            raise RuntimeError("simulated persistence failure")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_commit):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    assert run.success is False
    assert "persistence_error" in run.output_summary

    db_session.expire_all()
    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
    assert rows == []  # no half-written Evidence
    refreshed_opp = db_session.get(Opportunity, opp.id)
    assert refreshed_opp.research_summary is None  # no half-written summary either


def test_worker_failure_writes_no_evidence_and_no_summary(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _fail_worker_result("timeout", "researcher exceeded timeout"),
    )
    assert run.success is False
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []
    db_session.refresh(opp)
    assert opp.research_summary is None


def test_malformed_worker_output_writes_no_evidence_and_no_summary(db_session):
    opp = _make_opportunity(db_session)
    worker_result = WorkerResult(
        ok=True, exit_code=0, session_id="s1", result_text="not valid json at all",
        usage={}, total_cost_usd=0.01, is_error=False, error_kind=None,
        error_detail=None, stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)
    assert run.success is False
    assert "unusable_payload" in run.output_summary
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []


# ---------------------------------------------------------------------------
# 22-24: cost / usage bookkeeping
# ---------------------------------------------------------------------------

def test_22_costevent_untouched(db_session):
    opp = _make_opportunity(db_session)
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result())
    assert db_session.scalars(select(CostEvent)).all() == []


def test_23_agentrun_cost_eur_stays_zero(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(total_cost_usd=3.5),
    )
    assert run.cost_eur == 0.0


def test_24_usd_estimate_and_usage_logged_in_output_summary(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(
            total_cost_usd=1.234, usage={"input_tokens": 111, "output_tokens": 222}
        ),
    )
    assert "1.234" in run.output_summary
    assert "111" in run.output_summary and "222" in run.output_summary


def test_missing_cost_and_usage_does_not_break_dispatch(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(total_cost_usd=None, usage={}),
    )
    assert run.success is True


# ---------------------------------------------------------------------------
# 25-26: exactly one opportunity, exactly one model call, no silent rerun
# ---------------------------------------------------------------------------

def test_25_rerun_refused_when_research_summary_already_exists(db_session):
    opp = _make_opportunity(db_session)
    opp.research_summary = "Already researched."
    db_session.commit()

    with pytest.raises(ResearchAlreadyExistsError):
        dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result())

    # No new Evidence, no AgentRun, no silent no-op success
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []
    assert db_session.scalars(select(AgentRun)).all() == []


def test_unknown_opportunity_raises_not_found(db_session):
    with pytest.raises(OpportunityNotFoundError):
        dispatch_research(db_session, 999999, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result())


def test_26_exactly_one_model_call_per_dispatch(db_session):
    opp = _make_opportunity(db_session)
    call_count = {"n": 0}

    def counting_fn(**kwargs):
        call_count["n"] += 1
        return _ok_worker_result()

    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=counting_fn)
    assert call_count["n"] == 1


def test_exactly_one_agentrun_per_dispatch(db_session):
    opp = _make_opportunity(db_session)
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result())
    runs = db_session.scalars(select(AgentRun)).all()
    assert len(runs) == 1
    assert runs[0].agent_name == "researcher"
    assert runs[0].task_type == "opportunity_research"


def test_no_internal_retry_on_worker_failure(db_session):
    """A failed worker call must not be retried internally -- exactly one
    invocation, one AgentRun(success=False), then dispatch_research returns."""
    opp = _make_opportunity(db_session)
    call_count = {"n": 0}

    def counting_failing_fn(**kwargs):
        call_count["n"] += 1
        return _fail_worker_result("nonzero_exit")

    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=counting_failing_fn)
    assert call_count["n"] == 1
    assert run.success is False


def test_opportunity_status_and_scoring_untouched_by_research(db_session):
    """Research writes Evidence + research_summary only -- it must not
    silently mutate Opportunity.status or score (scoring is a separate,
    human/API-triggered concern, out of scope here)."""
    opp = _make_opportunity(db_session)
    assert opp.status.value == "discovered"
    assert opp.score is None

    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result())

    db_session.refresh(opp)
    assert opp.status.value == "discovered"
    assert opp.score is None


# ---------------------------------------------------------------------------
# Secret sanitization -- reusing M4.2's security discipline
# ---------------------------------------------------------------------------

_FAKE_SECRETS = {
    "anthropic_key": "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET123456",
    "bearer": "Bearer abcDEF1234567890xyzTOKENFAKEVALUE",
    "password_kv": "password=Sup3rFakeSecretPassw0rd!",
}


@pytest.mark.parametrize("label,secret", list(_FAKE_SECRETS.items()))
def test_secret_in_stderr_never_reaches_worker_result(label, secret):
    stderr = f"process failed: {secret} rejected"
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout="not json", stderr=stderr)):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert secret not in (result.stderr_excerpt or "")
    assert secret not in (result.error_detail or "")


@pytest.mark.parametrize("label,secret", list(_FAKE_SECRETS.items()))
def test_secret_in_failure_detail_never_reaches_agentrun_output_summary(db_session, label, secret):
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _fail_worker_result("nonzero_exit", f"failed, leaked {secret} here"),
    )
    assert secret not in run.output_summary


def test_session_identifiers_are_bounded_not_treated_as_secret_but_stored_safely():
    """session_id is not a secret (M3.2 brief: "session identifiers niet als
    geheim behandelen maar veilig begrenzen") -- it passes through as-is but
    the field itself is length-bounded at the DB column level; confirm a
    very long session_id doesn't crash parsing."""
    payload = json.dumps({"is_error": False, "session_id": "s" * 500, "result": "{}"})
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout=payload)):
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.session_id == "s" * 500  # not rejected, not redacted -- just not a secret
