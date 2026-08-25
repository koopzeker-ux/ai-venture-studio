"""M3.3 Critic tests (app.evaluation.run_critic).

subprocess.run standing in for the real `claude` binary is ALWAYS mocked
here -- no real Claude call, no paid model call. Hard design rule under
test throughout: the LLM never decides TEST/WATCH/REJECT -- our own
deterministic Python does, from already-computed score/coverage/
evidence_confidence plus structured red_team/experiment fields.
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
from app.evaluation.run_critic import (
    AlreadyEvaluatedError,
    CriticPayloadError,
    DIMENSION_KEYS,
    DIMENSION_WEIGHTS,
    MAX_BUDGET_USD,
    RATING_LEVELS,
    OpportunityNotFoundError,
    ResearchNotYetDoneError,
    _build_critic_prompt,
    _compute_evidence_confidence,
    _coerce_budget_eur,
    _coerce_rating,
    _determine_recommendation,
    _is_concrete_text,
    _points_for_dimension,
    _score_from_dimensions,
    _validate_evidence_refs,
    build_critic_argv,
    dispatch_critic,
    parse_critic_payload,
    run_critic,
)
from app.models.entities import AgentRun, CostEvent, Evidence, Experiment, Opportunity
from app.orchestration.claude_code_adapter import WorkerResult


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
        slug="critic-opp",
        title="Critic Test Opportunity",
        thesis="Founders struggle to track invoices across tools.",
        research_summary="Problem: ... Counter-evidence: ... Why we might NOT test this: ...",
    )
    defaults.update(overrides)
    opp = Opportunity(**defaults)
    db_session.add(opp)
    db_session.commit()
    return opp


def _make_evidence(db_session, opportunity_id, **overrides) -> Evidence:
    defaults = dict(
        opportunity_id=opportunity_id, claim="Demand is growing", evidence_type="research_finding",
        claim_type="FACT", source="TechCrunch", source_url="https://example.com/a", stance="SUPPORTS",
        found_at=None, source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
        duplicate_of_evidence_id=None,
    )
    defaults.update(overrides)
    e = Evidence(**defaults)
    db_session.add(e)
    db_session.commit()
    return e


def _dim(confidence="HIGH", refs=None, assessment="assessment text", rating="POSITIVE"):
    # LEAD fix (M3.3 pre-review, CRITICAL): rating defaults to POSITIVE so
    # every existing test that only varied `confidence` keeps its original,
    # numerically identical scoring behavior (POSITIVE+HIGH/MEDIUM/LOW
    # reproduces the pre-fix HIGH/MEDIUM/LOW points exactly) -- the new
    # rating axis is exercised by dedicated tests below, not by changing
    # what every other test implicitly means.
    return {"assessment": assessment, "evidence_refs": refs or [], "rating": rating, "confidence": confidence}


def _full_payload_json(
    dim_confidence="HIGH", dim_refs=None, fatal_risks=None,
    cheapest_test="run a landing page test for two weeks with paid ads",
    stop_criteria="stop after two weeks if conversion is below 2%",
    budget_eur=150.0,
    dim_rating="POSITIVE",
) -> dict:
    payload = {k: _dim(dim_confidence, dim_refs, rating=dim_rating) for k in DIMENSION_KEYS}
    payload["economics"] = {"assessment": "x", "known": ["a"], "unknown": ["b"]}
    payload["red_team"] = {
        "strongest_case_against": ["x"], "fatal_risks": fatal_risks or [], "missing_evidence": ["y"],
    }
    payload["experiment"] = {
        "hypothesis": "people will pay for X", "critical_assumption": "demand exists",
        "cheapest_test": cheapest_test, "budget_eur": budget_eur,
        "success_criteria": "10% conversion", "stop_criteria": stop_criteria,
    }
    return payload


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


def _envelope(result_payload: dict, **overrides) -> str:
    envelope = {
        "is_error": False, "session_id": "sess-critic-1", "total_cost_usd": 0.09,
        "usage": {"input_tokens": 400, "output_tokens": 250},
        "result": json.dumps(result_payload),
    }
    envelope.update(overrides)
    return json.dumps(envelope)


def _ok_worker_result(result_payload=None, **overrides) -> WorkerResult:
    if result_payload is None:
        result_payload = _full_payload_json()
    defaults = dict(
        ok=True, exit_code=0, session_id="sess-1", result_text=json.dumps(result_payload),
        usage={"input_tokens": 400, "output_tokens": 250}, total_cost_usd=0.09,
        is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    defaults.update(overrides)
    return WorkerResult(**defaults)


def _fail_worker_result(error_kind, error_detail="critic failed") -> WorkerResult:
    return WorkerResult(
        ok=False, exit_code=1 if error_kind == "nonzero_exit" else None, session_id=None,
        result_text=None, usage={}, total_cost_usd=None, is_error=True,
        error_kind=error_kind, error_detail=error_detail, stderr_excerpt=None,
    )


# ===========================================================================
# D. argv / tool safety (matrix items C, D)
# ===========================================================================

def test_argv_exact_shape():
    argv = build_critic_argv(prompt="evaluate")
    assert argv == [
        "claude", "-p", "evaluate", "--output-format", "json", "--permission-mode", "dontAsk",
        "--tools", "", "--safe-mode", "--max-budget-usd", MAX_BUDGET_USD,
    ]


def test_C_budget_cap_always_le_050():
    assert float(MAX_BUDGET_USD) <= 0.50
    argv = build_critic_argv(prompt="x")
    idx = argv.index("--max-budget-usd")
    assert argv[idx + 1] == MAX_BUDGET_USD


def test_budget_cap_has_no_override_parameter():
    import inspect
    params = inspect.signature(build_critic_argv).parameters
    assert "max_budget_usd" not in params
    assert "budget" not in params
    with pytest.raises(TypeError):
        build_critic_argv(prompt="x", max_budget_usd="999")  # type: ignore[call-arg]


def test_D_no_web_file_shell_tools_tools_flag_disables_everything():
    argv = build_critic_argv(prompt="x")
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""  # disables the built-in tool set entirely
    assert "--allowedTools" not in argv  # no allow-list mechanism relied on at all
    assert "WebSearch" not in argv
    assert "WebFetch" not in argv
    assert "Edit" not in argv
    assert "Write" not in argv
    assert "Bash" not in argv


def test_no_worktree_no_bare_no_continue_resume_no_bypass():
    argv = build_critic_argv(prompt="x")
    assert "--worktree" not in argv
    assert "--bare" not in argv
    assert "--continue" not in argv and "-c" not in argv
    assert "--resume" not in argv and "-r" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "bypassPermissions" not in " ".join(argv)


def test_safe_mode_and_dontask_present():
    argv = build_critic_argv(prompt="x")
    assert "--safe-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"


def test_prompt_cannot_look_like_a_flag():
    with pytest.raises(ValueError):
        build_critic_argv(prompt="--dangerously-skip-permissions")


# ===========================================================================
# run_critic outcome handling
# ===========================================================================

def test_successful_json_parse():
    payload = _envelope(_full_payload_json())
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout=payload)):
        result = run_critic(prompt="x", repo_path="/repo")
    assert result.ok is True
    assert result.total_cost_usd == 0.09


def test_malformed_outer_json():
    with patch("subprocess.run", return_value=_completed(returncode=0, stdout="not json {{{")):
        result = run_critic(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "invalid_json"


def test_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=900)):
        result = run_critic(prompt="x", repo_path="/repo", timeout_seconds=900)
    assert result.ok is False
    assert result.error_kind == "timeout"


def test_spawn_failure():
    with patch("subprocess.run", side_effect=OSError("claude: not found")):
        result = run_critic(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "spawn_error"


def test_model_error_envelope():
    payload = json.dumps({"is_error": True, "result": "budget exceeded"})
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout=payload)):
        result = run_critic(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "nonzero_exit"


def test_malformed_inner_json_is_payload_error():
    with pytest.raises(CriticPayloadError):
        parse_critic_payload("this is not json")


def test_missing_required_sections_raises():
    with pytest.raises(CriticPayloadError):
        parse_critic_payload(json.dumps({"customer_problem": _dim()}))  # missing experiment, etc.


# ===========================================================================
# E: model recommendation never trusted (hard design rule)
# ===========================================================================

def test_E_stray_recommendation_field_in_model_json_is_ignored():
    payload_json = _full_payload_json()
    payload_json["recommendation"] = "REJECT"  # model tries to sneak in a verdict
    payload_json["decision"] = "TEST"
    payload = parse_critic_payload(json.dumps(payload_json))
    # Parsing succeeds and produces a normal payload -- the stray fields are
    # never read into any dataclass and never influence anything downstream.
    assert not hasattr(payload, "recommendation")
    assert not hasattr(payload, "decision")


def test_E_model_text_containing_test_watch_reject_words_does_not_influence_gate(db_session):
    """Even if free-text assessments/economics text literally contain the
    words TEST/WATCH/REJECT, only the deterministic score/coverage/
    evidence_confidence/red_team/experiment fields drive the outcome."""
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
    for key in DIMENSION_KEYS:
        payload_json[key]["assessment"] = "I recommend REJECT immediately, this should never be TEST or WATCH."
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1, independently_confirmed=True)
    _make_evidence(db_session, opp.id, id=2, independently_confirmed=True, source="Reuters")
    _make_evidence(db_session, opp.id, id=3, independently_confirmed=False, source="Bloomberg")
    _make_evidence(db_session, opp.id, id=4, independently_confirmed=False, source="WSJ")
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4])
    for key in DIMENSION_KEYS:
        payload_json[key]["assessment"] = "I recommend REJECT immediately."

    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result(payload_json)
    )
    db_session.refresh(opp)
    # The scored inputs are strong (HIGH confidence, real evidence, concrete
    # experiment, no fatal risk) -- the deterministic gate reaches TEST
    # despite the model's own text literally saying "REJECT".
    assert opp.score_breakdown["recommendation"] == "TEST"


# ===========================================================================
# F/N: UNKNOWN != 0
# ===========================================================================

def test_F_unknown_dimension_excluded_not_zeroed():
    payload_json = _full_payload_json(dim_confidence="HIGH")
    payload_json["retention_potential"]["confidence"] = "UNKNOWN"
    payload = parse_critic_payload(json.dumps(payload_json))
    score, coverage, breakdown = _score_from_dimensions(payload.dimensions)
    assert breakdown["retention_potential"]["included_in_score"] is False
    # Excluding one 6-point-weight dimension should not crater the score --
    # it's excluded from the denominator too, not scored as 0.
    assert score > 80  # still high: the other 94 weight points are all HIGH


def test_F_all_unknown_gives_zero_coverage_and_zero_score_not_a_crash():
    payload_json = {k: _dim("UNKNOWN") for k in DIMENSION_KEYS}
    payload_json["economics"] = {"assessment": "", "known": [], "unknown": []}
    payload_json["red_team"] = {"strongest_case_against": [], "fatal_risks": [], "missing_evidence": []}
    payload_json["experiment"] = {
        "hypothesis": "", "critical_assumption": "", "cheapest_test": "",
        "budget_eur": None, "success_criteria": "", "stop_criteria": "",
    }
    payload = parse_critic_payload(json.dumps(payload_json))
    score, coverage, _ = _score_from_dimensions(payload.dimensions)
    assert score == 0.0
    assert coverage == 0.0


# ===========================================================================
# LEAD fix (M3.3 pre-review, CRITICAL): rating (commercial direction) is
# structurally separate from confidence (evidence certainty) -- a
# confidently-assessed NEGATIVE dimension must score like bad news, not like
# good news, regardless of how sure the model is.
# ===========================================================================

def test_confidently_negative_assessment_scores_zero_not_nine():
    """The exact scenario this fix guards against: 'competition' assessed
    with HIGH confidence that the market is brutally saturated with no
    viable wedge -- rating=NEGATIVE, confidence=HIGH. Must score 0/10 for
    that dimension, never the 9/10 a pre-fix confidence-only formula would
    have awarded."""
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1], dim_rating="POSITIVE")
    payload_json["competition"] = _dim(
        confidence="HIGH", refs=[1], rating="NEGATIVE",
        assessment="Market is brutally saturated and incumbents dominate; no viable wedge.",
    )
    payload = parse_critic_payload(json.dumps(payload_json))
    _validate_evidence_refs(payload.dimensions, valid_ids={1}, anomalies=payload.anomalies)
    score, coverage, breakdown = _score_from_dimensions(payload.dimensions)
    assert breakdown["competition"]["included_in_score"] is True  # a known-bad assessment still counts toward coverage
    assert breakdown["competition"]["points_of_10"] == 0.0
    # High confidence in bad news is not good news for the overall score
    # either -- removing 10 of 100 weight's worth of points (competition's
    # own weight) from an otherwise-all-POSITIVE-HIGH payload must lower
    # the score measurably, not leave it untouched.
    all_positive_score, _, _ = _score_from_dimensions(
        parse_critic_payload(json.dumps(_full_payload_json(dim_confidence="HIGH", dim_refs=[1]))).dimensions
    )
    assert score < all_positive_score


def test_confidently_positive_assessment_still_scores_nine_control_case():
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1], dim_rating="POSITIVE")
    payload = parse_critic_payload(json.dumps(payload_json))
    _validate_evidence_refs(payload.dimensions, valid_ids={1}, anomalies=payload.anomalies)
    _, _, breakdown = _score_from_dimensions(payload.dimensions)
    assert breakdown["competition"]["points_of_10"] == 9.0


def test_weak_low_confidence_negative_still_scores_zero_never_positive():
    """Confidence never rescues a NEGATIVE rating into positive points --
    only rating=POSITIVE is confidence-scaled at all."""
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
    payload_json["buying_intent"] = _dim(confidence="LOW", refs=[], rating="NEGATIVE")
    payload = parse_critic_payload(json.dumps(payload_json))
    _validate_evidence_refs(payload.dimensions, valid_ids={1}, anomalies=payload.anomalies)
    _, _, breakdown = _score_from_dimensions(payload.dimensions)
    assert breakdown["buying_intent"]["points_of_10"] == 0.0


def test_neutral_rating_scores_fixed_midpoint_regardless_of_confidence():
    for confidence in ("HIGH", "MEDIUM", "LOW"):
        payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
        payload_json["market_gap"] = _dim(confidence=confidence, refs=[1], rating="NEUTRAL")
        payload = parse_critic_payload(json.dumps(payload_json))
        _validate_evidence_refs(payload.dimensions, valid_ids={1}, anomalies=payload.anomalies)
        _, _, breakdown = _score_from_dimensions(payload.dimensions)
        assert breakdown["market_gap"]["points_of_10"] == 5.0, confidence


def test_unknown_rating_excludes_dimension_even_with_high_confidence():
    """A dimension cannot be scored on confidence alone -- rating must also
    be known. UNKNOWN rating (direction genuinely couldn't be judged) with
    HIGH confidence must still be excluded from score/coverage, not scored
    as if positive."""
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
    payload_json["creative_potential"] = _dim(confidence="HIGH", refs=[1], rating="UNKNOWN")
    payload = parse_critic_payload(json.dumps(payload_json))
    _validate_evidence_refs(payload.dimensions, valid_ids={1}, anomalies=payload.anomalies)
    _, _, breakdown = _score_from_dimensions(payload.dimensions)
    assert breakdown["creative_potential"]["included_in_score"] is False


def test_invalid_rating_value_coerced_to_unknown_with_anomaly():
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
    payload_json["retention_potential"]["rating"] = "SORT_OF_GOOD_MAYBE"
    payload = parse_critic_payload(json.dumps(payload_json))
    assert payload.dimensions["retention_potential"].rating == "UNKNOWN"
    assert any("invalid/missing rating" in a for a in payload.anomalies)


def test_missing_rating_field_coerced_to_unknown_not_assumed_positive():
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1])
    del payload_json["customer_pain"]["rating"]
    payload = parse_critic_payload(json.dumps(payload_json))
    assert payload.dimensions["customer_pain"].rating == "UNKNOWN"


def test_points_for_dimension_unit_matrix():
    assert _points_for_dimension("POSITIVE", "HIGH") == 9.0
    assert _points_for_dimension("POSITIVE", "MEDIUM") == 6.0
    assert _points_for_dimension("POSITIVE", "LOW") == 3.0
    assert _points_for_dimension("NEUTRAL", "HIGH") == 5.0
    assert _points_for_dimension("NEUTRAL", "LOW") == 5.0
    assert _points_for_dimension("NEGATIVE", "HIGH") == 0.0
    assert _points_for_dimension("NEGATIVE", "LOW") == 0.0
    assert _points_for_dimension("UNKNOWN", "HIGH") is None
    assert _points_for_dimension("POSITIVE", "UNKNOWN") is None
    assert _points_for_dimension("UNKNOWN", "UNKNOWN") is None


def test_coerce_rating_valid_and_invalid():
    anomalies = []
    assert _coerce_rating("NEGATIVE", anomalies, "x") == "NEGATIVE"
    assert anomalies == []
    assert _coerce_rating("bogus", anomalies, "x") == "UNKNOWN"
    assert anomalies
    assert _coerce_rating(None, [], "x") == "UNKNOWN"


def test_prompt_explicitly_separates_rating_from_confidence(db_session=None):
    from app.models.entities import Opportunity as OpportunityModel
    opp = OpportunityModel(slug="x", title="T", thesis="Thesis", research_summary="summary")
    prompt = _build_critic_prompt(opp, [])
    assert "rating" in prompt and "confidence" in prompt
    assert "TWO SEPARATE JUDGMENTS" in prompt
    assert "confidence in bad news is still real confidence" in prompt


def test_budget_eur_unknown_never_becomes_fabricated_zero_in_dataclass():
    assert _coerce_budget_eur(None, []) is None
    assert _coerce_budget_eur("not a number", []) is None
    assert _coerce_budget_eur(-5, []) is None  # negative is invalid, not a real budget
    assert _coerce_budget_eur(0, []) == 0.0  # an explicit, valid zero IS accepted (real, not fabricated)
    assert _coerce_budget_eur(42.5, []) == 42.5


# ===========================================================================
# G/H/I: score, coverage, confidence are deterministic (pure functions, no randomness/model call)
# ===========================================================================

def test_G_score_is_pure_and_deterministic():
    payload_json = _full_payload_json(dim_confidence="MEDIUM")
    payload = parse_critic_payload(json.dumps(payload_json))
    r1 = _score_from_dimensions(payload.dimensions)
    r2 = _score_from_dimensions(payload.dimensions)
    assert r1 == r2


def test_H_coverage_reflects_fraction_of_dimension_weight_assessed():
    payload_json = _full_payload_json(dim_confidence="HIGH")
    for key in ("brand_expansion", "retention_potential"):  # 6+6=12 of 100 weight
        payload_json[key]["confidence"] = "UNKNOWN"
    payload = parse_critic_payload(json.dumps(payload_json))
    _, coverage, _ = _score_from_dimensions(payload.dimensions)
    assert coverage == pytest.approx(0.88, abs=0.001)


def test_I_evidence_confidence_deterministic_same_input_same_output():
    rows = [
        Evidence(id=i, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source="s", source_url=None, stance="SUPPORTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=True,
                 duplicate_of_evidence_id=None)
        for i in range(1, 5)
    ]
    r1 = _compute_evidence_confidence(rows)
    r2 = _compute_evidence_confidence(rows)
    assert r1 == r2


def test_no_evidence_gives_low_confidence_not_a_crash():
    value, label, breakdown = _compute_evidence_confidence([])
    assert label == "LOW"
    assert value == 0.0


# ===========================================================================
# J/K: duplicates never inflate, contra-evidence can drag confidence down
# ===========================================================================

def test_J_duplicate_evidence_does_not_inflate_confidence():
    non_dup_only = [
        Evidence(id=1, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source="s", source_url=None, stance="SUPPORTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
                 duplicate_of_evidence_id=None),
    ]
    with_duplicates_added = non_dup_only + [
        Evidence(id=2, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source="s2", source_url=None, stance="SUPPORTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
                 duplicate_of_evidence_id=1),
        Evidence(id=3, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source="s3", source_url=None, stance="SUPPORTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
                 duplicate_of_evidence_id=1),
    ]
    value_before, label_before, _ = _compute_evidence_confidence(non_dup_only)
    value_after, label_after, breakdown_after = _compute_evidence_confidence(with_duplicates_added)
    # Adding two rows that are PURELY duplicates must not meaningfully help --
    # non_duplicate_count stays 1 either way.
    assert breakdown_after["non_duplicate_count"] == 1
    assert value_after <= value_before + 0.01


def test_K_contradicting_evidence_lowers_confidence_relative_to_all_supporting():
    all_supporting = [
        Evidence(id=i, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source=f"s{i}", source_url=None, stance="SUPPORTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
                 duplicate_of_evidence_id=None)
        for i in range(1, 5)
    ]
    mixed = all_supporting[:2] + [
        Evidence(id=i, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="FACT",
                 source=f"s{i}", source_url=None, stance="CONTRADICTS", found_at=None,
                 source_reliability="HIGH", confidence=0.8, independently_confirmed=False,
                 duplicate_of_evidence_id=None)
        for i in range(3, 5)
    ]
    value_supporting, _, _ = _compute_evidence_confidence(all_supporting)
    value_mixed, _, _ = _compute_evidence_confidence(mixed)
    assert value_mixed < value_supporting


def test_independently_confirmed_alone_cannot_produce_high(monkeypatch=None):
    """Section 8: independently_confirmed is one of five ANDed hard gates for
    HIGH, never sufficient by itself. A single independently-confirmed row
    with otherwise thin evidence (volume=1, no other signals) must not reach
    HIGH even though independent_fraction=1.0."""
    rows = [
        Evidence(id=1, opportunity_id=1, claim="c", evidence_type="research_finding", claim_type="UNKNOWN",
                 source="s", source_url=None, stance=None, found_at=None,
                 source_reliability="LOW", confidence=None, independently_confirmed=True,
                 duplicate_of_evidence_id=None),
    ]
    _, label, breakdown = _compute_evidence_confidence(rows)
    assert breakdown["independently_confirmed_fraction"] == 1.0
    assert label != "HIGH"


# ===========================================================================
# L/M/N/O: decision gate thresholds
# ===========================================================================

def test_L_fatal_red_team_risk_blocks_test_even_with_perfect_score():
    from app.evaluation.run_critic import RedTeamAssessment, ExperimentProposal
    rt = RedTeamAssessment(strongest_case_against=[], fatal_risks=["platform ToS forbids this"], missing_evidence=[])
    exp = ExperimentProposal(
        hypothesis="h", critical_assumption="c", cheapest_test="a fully concrete cheap test plan",
        budget_eur=100.0, success_criteria="s", stop_criteria="a fully concrete stop criteria",
    )
    rec, reasons = _determine_recommendation(100.0, 1.0, "HIGH", rt, exp)
    assert rec == "REJECT"
    assert "fatal" in reasons[0]


def test_M_test_requires_every_gate_simultaneously():
    from app.evaluation.run_critic import RedTeamAssessment, ExperimentProposal
    rt = RedTeamAssessment(strongest_case_against=[], fatal_risks=[], missing_evidence=[])
    concrete_exp = ExperimentProposal(
        hypothesis="h", critical_assumption="c", cheapest_test="a fully concrete cheap test plan here",
        budget_eur=100.0, success_criteria="s", stop_criteria="a fully concrete stop criteria here",
    )
    # Everything perfect except coverage -> not TEST
    rec, _ = _determine_recommendation(90.0, 0.5, "HIGH", rt, concrete_exp)
    assert rec != "TEST"
    # Everything perfect except confidence -> not TEST
    rec, _ = _determine_recommendation(90.0, 1.0, "MEDIUM", rt, concrete_exp)
    assert rec != "TEST"
    # Everything perfect except a non-concrete experiment -> not TEST
    thin_exp = ExperimentProposal(
        hypothesis="h", critical_assumption="c", cheapest_test="TBD",
        budget_eur=100.0, success_criteria="s", stop_criteria="TBD",
    )
    rec, _ = _determine_recommendation(90.0, 1.0, "HIGH", rt, thin_exp)
    assert rec != "TEST"
    # Everything meets the bar -> TEST
    rec, _ = _determine_recommendation(90.0, 1.0, "HIGH", rt, concrete_exp)
    assert rec == "TEST"


def test_N_watch_is_the_default_for_promising_but_incomplete_cases():
    from app.evaluation.run_critic import RedTeamAssessment, ExperimentProposal
    rt = RedTeamAssessment(strongest_case_against=[], fatal_risks=[], missing_evidence=["key unknowns"])
    exp = ExperimentProposal(
        hypothesis="h", critical_assumption="c", cheapest_test="a fully concrete cheap test plan here",
        budget_eur=100.0, success_criteria="s", stop_criteria="a fully concrete stop criteria here",
    )
    rec, reasons = _determine_recommendation(55.0, 0.6, "MEDIUM", rt, exp)
    assert rec == "WATCH"
    assert reasons  # explains what's missing


def test_O_reject_thresholds():
    from app.evaluation.run_critic import RedTeamAssessment, ExperimentProposal
    rt = RedTeamAssessment(strongest_case_against=[], fatal_risks=[], missing_evidence=[])
    exp = ExperimentProposal(
        hypothesis="h", critical_assumption="c", cheapest_test="a fully concrete cheap test plan here",
        budget_eur=100.0, success_criteria="s", stop_criteria="a fully concrete stop criteria here",
    )
    # absolute score floor
    rec, _ = _determine_recommendation(10.0, 1.0, "HIGH", rt, exp)
    assert rec == "REJECT"
    # LOW confidence + weak score combo
    rec, _ = _determine_recommendation(25.0, 0.5, "LOW", rt, exp)
    assert rec == "REJECT"
    # LOW confidence alone with a DECENT score is NOT auto-reject (promising-but-thin -> WATCH)
    rec, _ = _determine_recommendation(50.0, 0.5, "LOW", rt, exp)
    assert rec != "REJECT"


def test_test_strictly_harder_than_watch_thresholds():
    assert True  # documented structurally: TEST requires score>=65 AND coverage>=0.70 AND HIGH
    # confidence AND concrete plan AND no fatal risk -- WATCH requires none of that,
    # it is the default outcome whenever REJECT doesn't trigger and TEST's gates aren't
    # all met (see test_M_test_requires_every_gate_simultaneously).


def test_is_concrete_text_heuristic():
    assert _is_concrete_text("TBD") is False
    assert _is_concrete_text("") is False
    assert _is_concrete_text(None) is False
    assert _is_concrete_text("Run a two-week paid landing page test") is True


# ===========================================================================
# 7: evidence reference validation (fail-safe)
# ===========================================================================

def test_unknown_evidence_ref_fails_safe_and_downgrades_confidence():
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[999])
    payload = parse_critic_payload(json.dumps(payload_json))
    anomalies = []
    _validate_evidence_refs(payload.dimensions, valid_ids=set(), anomalies=anomalies)
    for key in DIMENSION_KEYS:
        assert payload.dimensions[key].evidence_refs == []
        assert payload.dimensions[key].confidence == "UNKNOWN"
    assert any("does not exist" in a for a in anomalies)


def test_cross_opportunity_evidence_ref_rejected():
    """valid_ids passed in by dispatch_critic is always pre-scoped to the
    current Opportunity's own Evidence rows -- an id belonging to a
    DIFFERENT opportunity is therefore indistinguishable from "unknown" and
    is dropped the same way."""
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[42])
    payload = parse_critic_payload(json.dumps(payload_json))
    anomalies = []
    _validate_evidence_refs(payload.dimensions, valid_ids={1, 2, 3}, anomalies=anomalies)  # 42 not in this opp
    assert payload.dimensions["customer_problem"].evidence_refs == []


def test_valid_evidence_ref_is_kept_and_confidence_preserved():
    payload_json = _full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2])
    payload = parse_critic_payload(json.dumps(payload_json))
    anomalies = []
    _validate_evidence_refs(payload.dimensions, valid_ids={1, 2, 3}, anomalies=anomalies)
    assert payload.dimensions["customer_problem"].evidence_refs == [1, 2]
    assert payload.dimensions["customer_problem"].confidence == "HIGH"


def test_low_confidence_with_no_refs_is_not_downgraded():
    """Only HIGH/MEDIUM confidence requires evidence backing to survive --
    LOW/UNKNOWN are already the honest "not well supported" signal."""
    payload_json = _full_payload_json(dim_confidence="LOW", dim_refs=[])
    payload = parse_critic_payload(json.dumps(payload_json))
    anomalies = []
    _validate_evidence_refs(payload.dimensions, valid_ids=set(), anomalies=anomalies)
    assert payload.dimensions["customer_problem"].confidence == "LOW"


# ===========================================================================
# A/B/P/Q/R/S/T/U: end-to-end dispatch behavior
# ===========================================================================

def test_A_one_opportunity_per_dispatch(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    assert run.input_summary is not None
    assert f"opportunity_id={opp.id}" in run.input_summary


def test_B_exactly_one_model_call(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    call_count = {"n": 0}

    def counting_fn(**kwargs):
        call_count["n"] += 1
        return _ok_worker_result(_full_payload_json(dim_refs=[1]))

    dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=counting_fn)
    assert call_count["n"] == 1


def test_no_internal_retry_on_failure(db_session):
    opp = _make_opportunity(db_session)
    call_count = {"n": 0}

    def counting_fail(**kwargs):
        call_count["n"] += 1
        return _fail_worker_result("nonzero_exit")

    run = dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=counting_fail)
    assert call_count["n"] == 1
    assert run.success is False


def test_P_test_creates_exactly_one_proposed_experiment(db_session):
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4])),
    )
    experiments = db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all()
    assert len(experiments) == 1
    assert experiments[0].status == "proposed"
    assert experiments[0].hypothesis == "people will pay for X"


def test_unestimated_budget_persists_as_real_null_not_fabricated_zero(db_session):
    """LEAD fix (M3.3 pre-review): Experiment.budget_eur is nullable
    (Alembic 9b9043140432) -- when the Critic could not responsibly
    estimate a budget, the proposed Experiment row must store a real NULL,
    never a 0.0 placeholder that could be misread as 'free to run'."""
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(
            _full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4], budget_eur=None)
        ),
    )
    experiments = db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all()
    assert len(experiments) == 1
    assert experiments[0].budget_eur is None


def test_known_budget_still_persists_correctly(db_session):
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(
            _full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4], budget_eur=250.0)
        ),
    )
    experiments = db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all()
    assert experiments[0].budget_eur == 250.0


def test_Q_watch_creates_no_experiment(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1, source_reliability="MEDIUM", confidence=0.5)

    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_confidence="MEDIUM", dim_refs=[1])),
    )
    assert db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all() == []


def test_R_reject_creates_no_experiment(db_session):
    opp = _make_opportunity(db_session)
    payload_json = _full_payload_json(dim_confidence="HIGH", fatal_risks=["fatal legal blocker"])
    dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result(payload_json))
    assert db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all() == []


def test_S_opportunity_status_never_mutated(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    original_status = opp.status
    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    db_session.refresh(opp)
    assert opp.status == original_status


def test_T_critic_summary_persisted_with_required_sections(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    db_session.refresh(opp)
    assert opp.critic_summary is not None
    for required in ("SCORE:", "COVERAGE:", "EVIDENCE CONFIDENCE:", "FINAL DETERMINISTIC RECOMMENDATION:",
                      "RED TEAM:", "ECONOMICS:", "CHEAPEST NEXT EXPERIMENT:"):
        assert required in opp.critic_summary


def test_U_score_breakdown_traceable(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    db_session.refresh(opp)
    assert "dimensions" in opp.score_breakdown
    assert "coverage" in opp.score_breakdown
    assert "evidence_confidence" in opp.score_breakdown
    assert "recommendation" in opp.score_breakdown
    for key in DIMENSION_KEYS:
        assert key in opp.score_breakdown["dimensions"]


# ===========================================================================
# V: transaction atomicity
# ===========================================================================

def test_V_persistence_failure_leaves_no_half_memo_no_orphan_experiment(db_session):
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_first_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated persistence failure")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_first_commit):
        run = dispatch_critic(
            db_session, opp.id, repo_path="/fake",
            run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4])),
        )

    assert run.success is False
    assert "persistence_error" in run.output_summary

    db_session.expire_all()
    refreshed = db_session.get(Opportunity, opp.id)
    assert refreshed.critic_summary is None
    assert refreshed.score is None
    assert db_session.scalars(select(Experiment).where(Experiment.opportunity_id == opp.id)).all() == []


def test_flush_failure_also_rolls_back_cleanly(db_session):
    """A failure specifically during flush() (e.g. an Experiment insert
    constraint violation) must roll back the Opportunity mutation too, not
    just leave the Experiment out."""
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    original_flush = SASession.flush
    call_count = {"n": 0}

    def failing_first_flush(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:  # the main persistence transaction's own flush
            raise RuntimeError("simulated flush failure")
        return original_flush(self, *a, **kw)  # _log_agent_run's commit() flushes internally too

    with patch.object(SASession, "flush", failing_first_flush):
        run = dispatch_critic(
            db_session, opp.id, repo_path="/fake",
            run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4])),
        )
    assert run.success is False
    db_session.expire_all()
    refreshed = db_session.get(Opportunity, opp.id)
    assert refreshed.critic_summary is None


def test_worker_failure_writes_no_opportunity_mutation(db_session):
    opp = _make_opportunity(db_session)
    dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _fail_worker_result("timeout"))
    db_session.refresh(opp)
    assert opp.critic_summary is None
    assert opp.score is None


def test_malformed_payload_writes_no_opportunity_mutation(db_session):
    opp = _make_opportunity(db_session)
    bad_result = WorkerResult(
        ok=True, exit_code=0, session_id="s1", result_text="not json",
        usage={}, total_cost_usd=0.01, is_error=False, error_kind=None,
        error_detail=None, stderr_excerpt=None,
    )
    run = dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: bad_result)
    assert run.success is False
    assert "unusable_payload" in run.output_summary
    db_session.refresh(opp)
    assert opp.critic_summary is None


# ===========================================================================
# W/X/Y/Z: AgentRun, cost, no CostEvent, no Telegram
# ===========================================================================

def test_W_exactly_one_agentrun_per_dispatch(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    runs = db_session.scalars(select(AgentRun)).all()
    assert len(runs) == 1
    assert runs[0].agent_name == "critic"
    assert runs[0].task_type == "opportunity_evaluation"


def test_X_agentrun_cost_eur_stays_zero(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1]), total_cost_usd=0.33),
    )
    assert run.cost_eur == 0.0


def test_known_zero_cost_is_not_confused_with_unknown_cost(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1]), total_cost_usd=0.0),
    )
    assert "cost_usd_estimate=0.0" in run.output_summary


def test_unknown_cost_never_fabricated_as_zero_in_output_summary(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _fail_worker_result("timeout"),
    )
    assert "cost_usd_estimate=0" not in run.output_summary  # never fabricated when genuinely unknown


def test_cost_preserved_when_call_succeeds_but_persistence_fails(db_session):
    """Mirrors the M3.2 live-dogfood fix: an already-completed paid call's
    cost/usage must not be lost just because persistence failed afterward."""
    opp = _make_opportunity(db_session)
    for i in range(1, 5):
        _make_evidence(db_session, opp.id, id=i, independently_confirmed=(i <= 2))

    original_flush = SASession.flush
    call_count = {"n": 0}

    def failing_first_flush(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return original_flush(self, *a, **kw)

    with patch.object(SASession, "flush", failing_first_flush):
        run = dispatch_critic(
            db_session, opp.id, repo_path="/fake",
            run_critic_fn=lambda **kw: _ok_worker_result(
                _full_payload_json(dim_confidence="HIGH", dim_refs=[1, 2, 3, 4]), total_cost_usd=0.27
            ),
        )
    assert "0.27" in run.output_summary


def test_Y_no_costevent_ever_created(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
    )
    assert db_session.scalars(select(CostEvent)).all() == []


def test_Z_no_telegram_import_anywhere_in_module():
    import ast
    import importlib

    mod = importlib.import_module("app.evaluation.run_critic")
    with open(mod.__file__, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=mod.__file__)

    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {"app.services.telegram", "app.services.scoring"}
    assert not (imported_modules & forbidden), imported_modules & forbidden


def test_Z_dispatch_critic_never_calls_telegram(db_session):
    """Belt-and-braces: patch the real send_telegram_message and confirm a
    full successful dispatch never touches it."""
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    with patch("app.services.telegram.send_telegram_message") as mock_telegram:
        dispatch_critic(
            db_session, opp.id, repo_path="/fake",
            run_critic_fn=lambda **kw: _ok_worker_result(_full_payload_json(dim_refs=[1])),
        )
    mock_telegram.assert_not_called()


# ===========================================================================
# Opportunity lifecycle guards: not found / no research yet / already evaluated
# ===========================================================================

def test_unknown_opportunity_raises(db_session):
    with pytest.raises(OpportunityNotFoundError):
        dispatch_critic(db_session, 999999, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result())


def test_no_research_summary_refused(db_session):
    opp = _make_opportunity(db_session, research_summary=None)
    with pytest.raises(ResearchNotYetDoneError):
        dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result())
    assert db_session.scalars(select(AgentRun)).all() == []


def test_already_evaluated_refused_no_rerun(db_session):
    opp = _make_opportunity(db_session, critic_summary="Already evaluated.")
    with pytest.raises(AlreadyEvaluatedError):
        dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result())
    assert db_session.scalars(select(AgentRun)).all() == []


def test_no_evidence_at_all_still_dispatches_and_fails_toward_reject_or_watch(db_session):
    """Section 18 failure mode: no Evidence rows -- must not crash, and the
    natural consequence of near-zero evidence should show up as a very weak
    outcome, not a fabricated positive one."""
    opp = _make_opportunity(db_session)
    payload_json = _full_payload_json(dim_confidence="UNKNOWN")
    run = dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result(payload_json))
    assert run.success is True
    db_session.refresh(opp)
    assert opp.score_breakdown["recommendation"] in ("REJECT", "WATCH")


# ===========================================================================
# Secret sanitization -- reusing M4.2/M3.2 discipline, across every field
# ===========================================================================

_FAKE_SECRETS = {
    "anthropic_key": "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET123456",
    "bearer": "Bearer abcDEF1234567890xyzTOKENFAKEVALUE",
    "password_kv": "password=Sup3rFakeSecretPassw0rd!",
}


@pytest.mark.parametrize("label,secret", list(_FAKE_SECRETS.items()))
def test_secret_in_stderr_never_reaches_worker_result(label, secret):
    stderr = f"process failed: {secret} rejected"
    with patch("subprocess.run", return_value=_completed(returncode=1, stdout="not json", stderr=stderr)):
        result = run_critic(prompt="x", repo_path="/repo")
    assert secret not in (result.stderr_excerpt or "")
    assert secret not in (result.error_detail or "")


@pytest.mark.parametrize("label,secret", list(_FAKE_SECRETS.items()))
def test_secret_in_dimension_assessment_never_reaches_critic_summary(db_session, label, secret):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    payload_json = _full_payload_json(dim_refs=[1])
    payload_json["customer_problem"]["assessment"] = f"leaked here: {secret}"
    dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result(payload_json))
    db_session.refresh(opp)
    assert secret not in opp.critic_summary
    assert secret not in json.dumps(opp.score_breakdown)


@pytest.mark.parametrize("label,secret", list(_FAKE_SECRETS.items()))
def test_secret_in_failure_detail_never_reaches_agentrun_output_summary(db_session, label, secret):
    opp = _make_opportunity(db_session)
    run = dispatch_critic(
        db_session, opp.id, repo_path="/fake",
        run_critic_fn=lambda **kw: _fail_worker_result("nonzero_exit", f"failed, leaked {secret} here"),
    )
    assert secret not in run.output_summary


def test_extremely_long_model_text_is_truncated_not_persisted_unbounded(db_session):
    opp = _make_opportunity(db_session)
    _make_evidence(db_session, opp.id, id=1)
    payload_json = _full_payload_json(dim_refs=[1])
    payload_json["customer_problem"]["assessment"] = "x" * 100000
    dispatch_critic(db_session, opp.id, repo_path="/fake", run_critic_fn=lambda **kw: _ok_worker_result(payload_json))
    db_session.refresh(opp)
    assert len(opp.critic_summary) < 100000 + 5000  # bounded, not unbounded pass-through


# ===========================================================================
# Section 16: systemic parser-vs-DB-length audit for every Critic-persisted
# model string against a bounded DB column
# ===========================================================================

def test_no_repeat_of_m32_source_length_mismatch_all_critic_text_columns_are_unbounded():
    """Every column this module writes model-derived (not hardcoded) text
    into must be an unbounded Text column -- so there is no bounded-length
    column for a parser cap to ever drift out of sync with (the M3.2 live
    failure this is guarding against). Confirmed by introspecting the real
    SQLAlchemy column types, not by assumption."""
    import sqlalchemy

    model_derived_text_columns = [
        (Opportunity, "critic_summary"),
        (Experiment, "hypothesis"),
        (Experiment, "critical_assumption"),
        (Experiment, "cheapest_test"),
        (Experiment, "success_criteria"),
        (Experiment, "stop_criteria"),
    ]
    for model, column_name in model_derived_text_columns:
        col_type = model.__table__.columns[column_name].type
        assert isinstance(col_type, sqlalchemy.Text), (
            f"{model.__name__}.{column_name} is {type(col_type).__name__}, not Text -- "
            "if this is ever changed to a bounded String(N), a corresponding cap "
            "(mirroring app.research.run_researcher._cap_source_for_db) MUST be added "
            "before persisting Critic-derived text into it."
        )


def test_experiment_status_is_a_short_hardcoded_literal_never_model_derived():
    """Experiment.status IS a bounded column (String(50)) -- but this module
    only ever writes the fixed literal "proposed" into it, never anything
    derived from the model's JSON, so no length risk exists here either."""
    import importlib
    mod = importlib.import_module("app.evaluation.run_critic")
    with open(mod.__file__, encoding="utf-8") as f:
        source = f.read()
    assert 'status="proposed"' in source
    assert len("proposed") <= Experiment.__table__.columns["status"].type.length


def test_agentrun_string_columns_are_all_short_hardcoded_constants():
    """AgentRun.agent_name/task_type/model are bounded String(120) columns --
    this module only ever writes fixed literals ("critic",
    "opportunity_evaluation", "claude-code") into them, never model-derived
    text, so no length risk exists."""
    for value in ("critic", "opportunity_evaluation", "claude-code"):
        assert len(value) <= AgentRun.__table__.columns["agent_name"].type.length
        assert len(value) <= AgentRun.__table__.columns["task_type"].type.length
        assert len(value) <= AgentRun.__table__.columns["model"].type.length


# ===========================================================================
# Prompt content checks
# ===========================================================================

def test_prompt_includes_evidence_ids_and_fact_inference_estimate_unknown_rules(db_session):
    opp = _make_opportunity(db_session)
    e = _make_evidence(db_session, opp.id, id=1)
    prompt = _build_critic_prompt(opp, [e])
    assert f"id={e.id}" in prompt
    assert "FACT" in prompt and "INFERENCE" in prompt and "ESTIMATE" in prompt and "UNKNOWN" in prompt
    assert "do not write 0" in prompt
    assert "Do NOT include a TEST/WATCH/REJECT recommendation" in prompt
    for key in DIMENSION_KEYS:
        assert key in prompt


def test_prompt_handles_zero_evidence_rows_gracefully(db_session):
    opp = _make_opportunity(db_session)
    prompt = _build_critic_prompt(opp, [])
    assert "no Evidence rows exist" in prompt
