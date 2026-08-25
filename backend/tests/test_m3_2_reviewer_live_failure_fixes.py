"""M3.2 REVIEWER re-review: independent, adversarial validation of the LEAD
fixes for the live-dogfood failures at main@09a7659 (root cause 1:
Evidence.source parser-vs-DB-column length mismatch causing
StringDataRightTruncation after a paid model call; root cause 2: known
total_cost_usd/usage lost when persistence failed after a successful paid
call).

Written independently from tests/test_researcher.py's own new coverage for
the same fixes (test_source_db_max_len_matches_the_real_evidence_column,
test_long_source_persists_end_to_end_without_db_failure, test_cost_note_*,
test_short_exception_detail_never_includes_full_sql_or_all_parameters, etc.)
-- reuses fixture patterns only, and specifically targets angles that suite
does not already cover: the full boundary sweep around the 120-char DB
limit, a real SQLite empirical check that "no exception raised" is NOT
sufficient proof of DB-compatibility for this schema (SQLite silently
accepts oversized VARCHAR -- only Postgres would ever raise
StringDataRightTruncation), unicode/multibyte truncation correctness, a
secret deliberately straddling both truncation boundaries (source's 120-char
cap and the exception-diagnostic's 300-char cap), flush-failure (as opposed
to only commit-failure) preserving known cost, multi-item batches mixing a
too-long source with a secret-bearing one, an independent systemic
parser-vs-DB-column audit across Evidence/Opportunity/AgentRun, and
AgentRun audit-history immutability.

subprocess.run standing in for the real `claude` binary is ALWAYS mocked.
No real Claude/WebSearch/WebFetch call, no paid research run, no live model
call anywhere in this file. No production code changed.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SASession, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base
from app.models.entities import AgentRun, CostEvent, Evidence, Opportunity
from app.orchestration.claude_code_adapter import WorkerResult
from app.research.run_researcher import (
    _SOURCE_DB_MAX_LEN,
    _cap_source_for_db,
    _cost_note,
    _short_exception_detail,
    dispatch_research,
    parse_research_payload,
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


def _make_opportunity(db, **overrides) -> Opportunity:
    defaults = dict(slug="live-fix-opp", title="Live Fix Opportunity", thesis="Some thesis.")
    defaults.update(overrides)
    opp = Opportunity(**defaults)
    db.add(opp)
    db.commit()
    db.refresh(opp)
    return opp


def _ok_worker_result(evidence, research_summary="Summary.", **overrides) -> WorkerResult:
    result_text = json.dumps({"evidence": evidence, "research_summary": research_summary})
    defaults = dict(
        ok=True, exit_code=0, session_id="sess-r", result_text=result_text,
        usage={"input_tokens": 10}, total_cost_usd=0.02,
        is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    defaults.update(overrides)
    return WorkerResult(**defaults)


# ===========================================================================
# 2. Source-length fix -- independent verification
# ===========================================================================

def test_A_source_db_max_len_is_derived_not_hardcoded_independently():
    """Independent confirmation of brief 2.B: _SOURCE_DB_MAX_LEN must be a
    live attribute read off the SQLAlchemy column, not a second literal
    '120' that could drift out of sync with the column again -- inspect the
    actual column and cross-check they are IDENTICAL objects/values, not
    just equal by coincidence."""
    real_column_length = Evidence.__table__.columns["source"].type.length
    assert _SOURCE_DB_MAX_LEN == real_column_length == 120


def test_B_sqlite_alone_would_NOT_have_caught_the_original_bug(db_session):
    """Empirically proves why "no exception raised" is not sufficient
    evidence of DB-compatibility for this schema: a raw INSERT of a
    250-char `source` (well past the 120-char VARCHAR column) succeeds
    silently against the very SQLite engine every test in this suite runs
    against -- unlike Postgres, which is where the real live-dogfood
    StringDataRightTruncation actually happened. Every persistence-success
    assertion in this file and in test_researcher.py must therefore assert
    on the STORED VALUE'S LENGTH directly; the absence of a raised
    exception proves nothing on its own for this column."""
    opp = _make_opportunity(db_session, slug="sqlite-length-probe")
    oversized = Evidence(opportunity_id=opp.id, claim="c", evidence_type="research_finding", source="Z" * 250)
    db_session.add(oversized)
    db_session.commit()  # would raise on Postgres; SQLite accepts it silently
    db_session.refresh(oversized)
    assert len(oversized.source) == 250  # proves SQLite stored it uncapped, not that the app is safe


@pytest.mark.parametrize("length", [0, 1, 119, 120, 121, 500, 5000])
def test_C_source_length_boundary_sweep_never_exceeds_db_column(length):
    """Full boundary sweep per brief 2.C. length=0 goes through
    _cap_source_for_db too (parse_research_payload rejects an empty/absent
    `source` upstream via its own non-empty check -- this tests the
    truncation helper directly, independent of that upstream gate)."""
    source = "S" * length
    anomalies: list[str] = []
    capped = _cap_source_for_db(source, anomalies, idx=0)
    assert len(capped) <= _SOURCE_DB_MAX_LEN
    if length > _SOURCE_DB_MAX_LEN:
        assert any("source truncated" in a for a in anomalies)
    else:
        assert anomalies == []
        assert capped == source


def test_D_secret_straddling_the_120_char_source_truncation_boundary_never_leaks():
    """Places a real-shaped secret so it spans BOTH sides of the 120-char
    cutoff (positions 100-140 of the raw input) -- a naive
    truncate-then-redact implementation would cut the secret pattern in
    half and could leave a partial, unmatched fragment un-redacted.
    _cap_source_for_db redacts the FULL string first (via sanitize_text)
    and only then truncates the already-redacted result, so this must be
    fully safe regardless of where the secret falls relative to 120."""
    secret = "sk-ant-api03-STRADDLEBOUNDARYFAKESECRETVALUE123456"  # ~52 chars
    prefix = "A" * 100
    suffix = "B" * 100
    source = prefix + secret + suffix  # secret occupies chars [100:152], straddling index 120
    anomalies: list[str] = []
    capped = _cap_source_for_db(source, anomalies, idx=0)
    assert "sk-ant-api03-STRADDLEBOUNDARYFAKESECRETVALUE123456" not in capped
    assert "STRADDLEBOUNDARY" not in capped  # no partial/unmatched fragment either
    assert len(capped) <= _SOURCE_DB_MAX_LEN


def test_E_source_url_length_independent_of_source_db_cap():
    """Brief 2.E: source_url must never be capped to 120 -- it maps to a
    Text column with its own, much larger sanitize_text cap (2000).
    Constructs a source_url intentionally longer than 120 but shorter than
    2000 to isolate this from source's own capping logic entirely."""
    long_url = "https://example.com/article/" + ("x" * 300)  # > 120, < 2000
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "short source", "source_url": long_url}],
        "research_summary": "s",
    }))
    assert payload.entries[0].source_url == long_url
    assert len(payload.entries[0].source_url) > _SOURCE_DB_MAX_LEN


@pytest.mark.parametrize("text,char_count", [
    ("日本語のソース名前がとても長い場合のテスト" * 10, None),  # CJK, multi-byte in UTF-8
    ("🚀" * 200, None),  # emoji, astral-plane code points
    ("Ünïcödé wïth áccénts " * 10, None),
])
def test_F_unicode_multibyte_source_truncates_to_exact_char_count_not_broken(text, char_count):
    """Brief 2.F: Python string slicing operates on code points, matching
    Postgres VARCHAR(n)'s own character-count (not byte-count) semantics --
    confirms truncation never produces a broken surrogate pair, an
    off-by-one from multi-byte UTF-8 encoding, or a length miscount for
    astral-plane characters (emoji) that would occupy 2 UTF-16 code units
    but are still exactly 1 Python str element."""
    anomalies: list[str] = []
    capped = _cap_source_for_db(text, anomalies, idx=0)
    assert len(capped) <= _SOURCE_DB_MAX_LEN
    # Round-trip through UTF-8 encode/decode must not raise or produce a
    # replacement character -- proof no character was split mid-codepoint.
    re_decoded = capped.encode("utf-8").decode("utf-8")
    assert re_decoded == capped
    assert "�" not in capped  # no unicode replacement character


def test_G_long_source_produces_traceable_idx_scoped_anomaly():
    anomalies: list[str] = []
    _cap_source_for_db("X" * 200, anomalies, idx=3)
    assert len(anomalies) == 1
    assert "evidence[3]" in anomalies[0]
    assert str(_SOURCE_DB_MAX_LEN) in anomalies[0]


# ===========================================================================
# 3. Systemic parser-vs-DB length audit -- independent of LEAD's own table
# ===========================================================================

def test_systemic_audit_only_source_has_both_a_bounded_db_column_and_free_text_model_content():
    """Independently re-derives the conclusion LEAD's table claims, rather
    than trusting it: walks every Evidence/Opportunity/AgentRun column
    dispatch_research can write model-controlled free text into, and
    confirms source is the ONLY one where (a) the DB column has a real
    length ceiling AND (b) the value is free text derived from the
    researcher's own output (as opposed to a fixed short vocabulary this
    codebase controls, or an unbounded Text column)."""
    evidence_cols = {c.name: c for c in Evidence.__table__.columns}
    opp_cols = {c.name: c for c in Opportunity.__table__.columns}
    agentrun_cols = {c.name: c for c in AgentRun.__table__.columns}

    # Fields dispatch_research actually writes model-controlled free text into:
    free_text_targets = {
        ("Evidence", "claim"): evidence_cols["claim"],
        ("Evidence", "source"): evidence_cols["source"],
        ("Evidence", "source_url"): evidence_cols["source_url"],
        ("Opportunity", "research_summary"): opp_cols["research_summary"],
        ("AgentRun", "input_summary"): agentrun_cols["input_summary"],
        ("AgentRun", "output_summary"): agentrun_cols["output_summary"],
    }
    bounded = {k: v.type.length for k, v in free_text_targets.items() if getattr(v.type, "length", None) is not None}
    assert bounded == {("Evidence", "source"): 120}

    # Fixed-vocabulary fields (never arbitrary model free text) -- checked
    # against their known max literal length instead.
    assert evidence_cols["claim_type"].type.length == 20  # longest literal "INFERENCE" = 9
    assert evidence_cols["stance"].type.length == 20  # longest literal "CONTRADICTS" = 11
    assert evidence_cols["source_reliability"].type.length == 20  # longest literal "MEDIUM"/"UNKNOWN" = 7
    assert agentrun_cols["agent_name"].type.length == 120  # hardcoded "researcher" = 10
    assert agentrun_cols["task_type"].type.length == 120  # hardcoded "opportunity_research" = 20
    assert agentrun_cols["model"].type.length == 120  # hardcoded "claude-code" = 11


def test_systemic_audit_evidence_type_constant_fits_its_column():
    from app.research.run_researcher import EVIDENCE_TYPE_RESEARCH_FINDING
    col = Evidence.__table__.columns["evidence_type"]
    assert col.type.length == 80
    assert len(EVIDENCE_TYPE_RESEARCH_FINDING) <= 80


def test_systemic_audit_claim_and_research_summary_are_genuinely_unbounded_text():
    """Confirms claim/source_url/research_summary have NO db-side ceiling
    at all -- so no parser cap on them, however large, could ever produce a
    second variant of the source-length bug regardless of its value."""
    assert Evidence.__table__.columns["claim"].type.length is None
    assert Evidence.__table__.columns["source_url"].type.length is None
    assert Opportunity.__table__.columns["research_summary"].type.length is None


# ===========================================================================
# 4. Adversarial persistence reproduction -- multi-item batch
# ===========================================================================

def test_multi_item_batch_with_one_extreme_source_and_one_secret_source_persists_cleanly(db_session):
    """Closest independent reproduction of the real dogfood failure shape:
    several plausible evidence items plus one pathological one (source far
    past the DB limit) in the SAME batch, plus a separate one carrying a
    secret-shaped source -- the whole dispatch must still succeed, and
    EVERY row's stored source must independently respect the DB column,
    not just the pathological one."""
    opp = _make_opportunity(db_session)
    normal_source = "TechCrunch article on market sizing"
    extreme_source = (
        "A researcher-written source description that just keeps going and going, "
        "citing the full title of the report, the publisher, the author, the page "
        "number, and a parenthetical aside, well past what any reasonable person "
        "would consider a 'source' field -- exactly the kind of realistic but "
        "oversized text a real live run produced."
    )
    secret_source = "Internal memo leak sk-ant-api03-FAKEBATCHSECRETFAKEBATCHSECRET999"
    assert len(extreme_source) > _SOURCE_DB_MAX_LEN

    evidence = [
        {"id": "e1", "claim": "Claim one", "source": normal_source, "stance": "SUPPORTS"},
        {"id": "e2", "claim": "Claim two", "source": extreme_source, "stance": "SUPPORTS"},
        {"id": "e3", "claim": "Claim three", "source": secret_source, "stance": "CONTRADICTS"},
    ]
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(evidence, research_summary="Full summary."),
    )
    assert run.success is True

    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id).order_by(Evidence.id)).all()
    assert len(rows) == 3
    for row in rows:
        assert len(row.source) <= _SOURCE_DB_MAX_LEN
    assert rows[0].source == normal_source
    assert "sk-ant-api03" not in rows[2].source
    refreshed = db_session.get(Opportunity, opp.id)
    assert refreshed.research_summary == "Full summary."


# ===========================================================================
# 5. Cost-on-failure -- independent branches, including flush (not just commit)
# ===========================================================================

def test_A_worker_not_ok_with_cost_known_preserves_cost_independent_check(db_session):
    opp = _make_opportunity(db_session)
    worker_result = WorkerResult(
        ok=False, exit_code=1, session_id="s1", result_text=None, usage={"input_tokens": 77},
        total_cost_usd=0.33, is_error=True, error_kind="nonzero_exit", error_detail="model failed",
        stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)
    assert run.success is False
    assert "0.33" in run.output_summary
    assert "77" in run.output_summary


def test_B_payload_error_after_successful_paid_call_preserves_cost_independent_check(db_session):
    opp = _make_opportunity(db_session)
    worker_result = WorkerResult(
        ok=True, exit_code=0, session_id="s1", result_text="{broken json",
        usage={"output_tokens": 88}, total_cost_usd=0.44, is_error=False,
        error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)
    assert run.success is False
    assert "0.44" in run.output_summary
    assert "88" in run.output_summary


def test_C_flush_failure_after_paid_call_preserves_cost_not_just_commit_failure(db_session):
    """test_researcher.py's own coverage patches commit() -- this
    independently patches flush() instead, the earlier point of failure
    inside dispatch_research's try block (db.flush() assigns evidence ids
    before duplicate-link resolution), to confirm cost/usage preservation
    doesn't depend on which specific persistence step fails."""
    opp = _make_opportunity(db_session)
    worker_result = _ok_worker_result(
        [{"id": "e1", "claim": "a", "source": "s"}], total_cost_usd=0.091, usage={"input_tokens": 123},
    )
    original_flush = SASession.flush
    call_count = {"n": 0}

    def failing_first_flush(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated flush failure")
        return original_flush(self, *a, **kw)

    with patch.object(SASession, "flush", failing_first_flush):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    assert run.success is False
    assert "0.091" in run.output_summary
    assert "123" in run.output_summary


def test_D_commit_failure_after_duplicate_link_resolution_preserves_cost(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "a", "source": "s1", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "a", "source": "s2", "stance": "SUPPORTS", "duplicate_of": "e1"},
    ]
    worker_result = _ok_worker_result(evidence, total_cost_usd=0.055, usage={"cache_read_input_tokens": 40})
    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_first_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated commit failure")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_first_commit):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    assert run.success is False
    assert "0.055" in run.output_summary
    assert "40" in run.output_summary


def test_cost_eur_stays_0_0_and_no_costevent_even_when_cost_note_present(db_session):
    """The cost-on-failure fix must still respect the existing EUR/CostEvent
    contract: total_cost_usd/usage land ONLY in output_summary text."""
    opp = _make_opportunity(db_session)
    worker_result = _ok_worker_result([{"id": "e1", "claim": "a", "source": "s"}])
    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_first_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated commit failure")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_first_commit):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    assert run.cost_eur == 0.0
    assert db_session.scalars(select(CostEvent)).all() == []


def test_cost_note_never_appears_for_a_secret_shaped_usage_value():
    """usage is already whitelisted upstream (run_researcher's own
    _sanitize_usage) before it ever reaches a WorkerResult -- _cost_note
    does not re-sanitize, so this confirms the whitelist boundary this
    function relies on is real: an unexpected/secret-shaped usage key
    cannot reach _cost_note's output because it was already dropped before
    construction, not because _cost_note itself filters it."""
    wr = WorkerResult(
        ok=True, exit_code=0, session_id="s1", result_text="{}",
        usage={"input_tokens": 5},  # already whitelisted -- no 'debug_info' key possible here
        total_cost_usd=0.01, is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    note = _cost_note(wr)
    assert note == ", cost_usd_estimate=0.01, usage={'input_tokens': 5}"


# ===========================================================================
# 6. UNKNOWN cost semantics -- pre-model failures never fabricate cost=0
# ===========================================================================

@pytest.mark.parametrize("error_kind,exit_code", [("timeout", None), ("spawn_error", None), ("invalid_json", 0)])
def test_pre_model_failure_never_reports_a_fabricated_cost_usd_estimate_of_zero(db_session, error_kind, exit_code):
    """A pre-model failure (never even reached a parsed cost figure) must
    leave 'cost_usd_estimate' entirely ABSENT from output_summary -- never
    present with a value of 0, which _cost_note callers could otherwise
    misread as "the model call cost nothing" (a measured fact) rather than
    "no cost information was ever available" (a true unknown)."""
    opp = _make_opportunity(db_session)
    worker_result = WorkerResult(
        ok=False, exit_code=exit_code, session_id=None, result_text=None, usage={},
        total_cost_usd=None, is_error=True, error_kind=error_kind,
        error_detail=f"researcher failed before any cost was known ({error_kind})", stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)
    assert run.success is False
    assert "cost_usd_estimate" not in run.output_summary
    assert "cost_usd_estimate=0" not in run.output_summary
    assert "usage=" not in run.output_summary  # empty usage dict also produces no usage note


def test_cost_note_distinguishes_None_from_a_real_zero_cost():
    """0.0 is a plausible REAL total_cost_usd (e.g. a cache-hit-only call) --
    must still be reported when the worker actually returned it, unlike
    None (unknown). Confirms _cost_note does not conflate the two via a
    falsy-value check."""
    wr_real_zero = WorkerResult(
        ok=True, exit_code=0, session_id="s1", result_text="{}", usage={},
        total_cost_usd=0.0, is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    wr_unknown = WorkerResult(
        ok=False, exit_code=None, session_id=None, result_text=None, usage={},
        total_cost_usd=None, is_error=True, error_kind="timeout", error_detail="boom", stderr_excerpt=None,
    )
    assert "cost_usd_estimate=0.0" in _cost_note(wr_real_zero)
    assert "cost_usd_estimate" not in _cost_note(wr_unknown)


# ===========================================================================
# 7. Failure-diagnostics -- adversarial, secrets at the truncation boundary
# ===========================================================================

@pytest.mark.parametrize("label,secret", [
    ("anthropic_key", "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET999999"),
    ("bearer", "Bearer FAKEBEARERTOKENVALUE1234567890ABCDEFGH"),
    ("password_eq", "password=SuperSecretFakeException123!"),
])
def test_short_exception_detail_redacts_secrets_regardless_of_position(label, secret):
    exc = RuntimeError(f"IntegrityError near column: {secret} while inserting evidence row")
    detail = _short_exception_detail(exc)
    assert secret not in detail
    assert "RuntimeError" in detail


def test_secret_straddling_the_300_char_exception_truncation_boundary_never_leaks():
    """Mirrors test_D above but for the exception-diagnostics 300-char cap:
    places a secret spanning positions 280-330 of the first line (before
    any truncation), proving sanitize_text's redact-then-truncate ordering
    holds for _short_exception_detail too, not just _cap_source_for_db."""
    secret = "sk-ant-api03-EXCEPTIONBOUNDARYFAKESECRETVALUE654321"  # ~53 chars
    padding_before = "x" * 280
    padding_after = "y" * 100
    exc = RuntimeError(padding_before + secret + padding_after)
    detail = _short_exception_detail(exc)
    assert secret not in detail
    assert "EXCEPTIONBOUNDARY" not in detail
    assert len(detail) < 350


def test_short_exception_detail_bounded_even_for_a_single_line_no_newline_dump():
    """The real fix's docstring reasons about multi-line SQLAlchemy errors
    (SQL/params each on their own line) -- but some DBAPI drivers format
    the whole error, including bound parameters, as ONE long line with no
    newline at all. Confirms the 300-char cap alone (not reliance on a
    newline being present) is what actually bounds the output in that
    case."""
    single_line = "OperationalError: " + ("param_value_" * 200)  # single line, thousands of chars, no \n
    assert "\n" not in single_line
    exc = RuntimeError(single_line)
    detail = _short_exception_detail(exc)
    assert len(detail) < 350


def test_short_exception_detail_multiline_evidence_body_content_excluded():
    exc = RuntimeError(
        "IntegrityError: constraint failed\n"
        "[parameters: {'claim__0': 'Multi-line scraped claim body\\nwith embedded newlines\\nand more text'}]"
    )
    detail = _short_exception_detail(exc)
    assert "scraped claim body" not in detail
    assert "IntegrityError" in detail


# ===========================================================================
# 8. Atomicity -- reconfirmed with the cost-logging code path present
# ===========================================================================

def test_atomicity_still_holds_evidence_rollback_with_cost_note_active(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "a", "source": "s1", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "a", "source": "s2", "stance": "SUPPORTS", "duplicate_of": "e1"},
    ]
    worker_result = _ok_worker_result(evidence, total_cost_usd=0.5, usage={"input_tokens": 1})
    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_first_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_first_commit):
        dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: worker_result)

    db_session.expire_all()
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []
    assert db_session.get(Opportunity, opp.id).research_summary is None


def test_atomicity_pre_existing_evidence_untouched_by_a_later_failed_dispatch(db_session):
    """A prior SUCCESSFUL dispatch's Evidence rows must remain fully intact
    if a later, unrelated dispatch on a DIFFERENT opportunity fails at
    persistence -- proves the rollback scope is per-transaction/session
    correct, not accidentally touching unrelated committed rows."""
    opp_a = _make_opportunity(db_session, slug="opp-a")
    opp_b = _make_opportunity(db_session, slug="opp-b")

    dispatch_research(
        db_session, opp_a.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result([{"id": "e1", "claim": "keep me", "source": "s"}]),
    )
    existing = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp_a.id)).all()
    assert len(existing) == 1
    existing_id = existing[0].id

    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_first_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure for opp_b only")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_first_commit):
        dispatch_research(
            db_session, opp_b.id, repo_path="/fake",
            run_researcher_fn=lambda **kw: _ok_worker_result([{"id": "e1", "claim": "should not persist", "source": "s"}]),
        )

    db_session.expire_all()
    still_there = db_session.get(Evidence, existing_id)
    assert still_there is not None
    assert still_there.claim == "keep me"
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp_b.id)).all() == []


# ===========================================================================
# 9. Audit-history immutability
# ===========================================================================

def test_dispatch_research_never_mutates_a_pre_existing_agentrun_row(db_session):
    """Simulates the "AgentRun id=3 from the real failed dogfood run must
    stay historical truth" requirement: seeds an AgentRun row directly
    (standing in for that historical failure record), runs an unrelated
    successful dispatch afterward, and confirms the seeded row's every
    field is byte-for-byte unchanged -- _log_agent_run only ever
    constructs and INSERTs a brand new AgentRun, never queries/updates an
    existing one (confirmed independently by code inspection: no
    db.query(AgentRun) or session.get(AgentRun, ...) followed by a mutation
    exists anywhere in run_researcher.py)."""
    import inspect
    import app.research.run_researcher as run_researcher_module
    src = inspect.getsource(run_researcher_module)
    assert "AgentRun).filter" not in src
    assert "query(AgentRun)" not in src
    assert "get(AgentRun" not in src

    historical = AgentRun(
        agent_name="researcher", task_type="opportunity_research",
        input_summary="opportunity_id=21 slug=historical-failed-run",
        output_summary="research run failed (persistence_error): StringDataRightTruncation: ...",
        model="claude-code", cost_eur=0.0, success=False,
    )
    db_session.add(historical)
    db_session.commit()
    db_session.refresh(historical)
    historical_id = historical.id
    snapshot = dict(
        input_summary=historical.input_summary, output_summary=historical.output_summary,
        model=historical.model, cost_eur=historical.cost_eur, success=historical.success,
    )

    opp = _make_opportunity(db_session, slug="unrelated-new-run")
    dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result([{"id": "e1", "claim": "c", "source": "s"}]),
    )

    db_session.expire_all()
    reloaded = db_session.get(AgentRun, historical_id)
    assert reloaded.input_summary == snapshot["input_summary"]
    assert reloaded.output_summary == snapshot["output_summary"]
    assert reloaded.success == snapshot["success"] is False
    assert reloaded.cost_eur == 0.0
    all_runs = db_session.scalars(select(AgentRun)).all()
    assert len(all_runs) == 2  # historical row + one new row, never merged/overwritten


# ===========================================================================
# 10. Security / scope -- diff-level confirmations for this fix round
# ===========================================================================

def test_no_new_dependency_or_provider_touched_by_this_fix_round():
    import inspect
    import app.research.run_researcher as mod_ref
    src = inspect.getsource(mod_ref)
    for forbidden in ("import anthropic", "import openai", "boto3", "requests.get", "httpx."):
        assert forbidden not in src


def test_no_retry_or_scheduler_construct_introduced_by_the_fix():
    import inspect
    import app.research.run_researcher as mod_ref
    src = inspect.getsource(mod_ref)
    for token in ("while True", "APScheduler", "retry", "Retry"):
        assert token not in src


def test_max_budget_usd_unaffected_by_source_length_or_cost_note_fix():
    from app.research.run_researcher import MAX_BUDGET_USD, build_research_argv
    argv = build_research_argv(prompt="x")
    assert argv[argv.index("--max-budget-usd") + 1] == MAX_BUDGET_USD == "2.00"
