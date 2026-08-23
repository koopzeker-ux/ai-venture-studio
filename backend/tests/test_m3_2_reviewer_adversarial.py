"""M3.2 REVIEWER: independent, adversarial validation of the Researcher
vertical slice (app.research.run_researcher, GET /opportunities/{id}/dossier,
the Evidence/Opportunity schema additions).

Written independently from the BUILDER/INTELLIGENCE test suites
(tests/test_researcher.py, tests/test_opportunity_dossier.py,
tests/test_m3_2_evidence_migration.py) -- reuses their fixture *patterns*
(db_session / client) but not their assertions, and specifically targets
angles those suites do not already cover: cross-opportunity duplicate refs,
duplicate chains, forward/duplicate temp-id references, the honesty gap
between "confidence=0.5 genuinely estimated" and "confidence=0.5 because the
researcher declined to estimate", and -- the largest section here -- what the
hallucination-defense prompt claims versus what parse_research_payload /
dispatch_research technically enforce.

subprocess.run standing in for the real `claude` binary is ALWAYS mocked.
No real Claude/WebSearch/WebFetch call, no paid research run, no live model
call anywhere in this file.
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session as SASession, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import Base, get_db as get_db_dep
from app.main import app
from app.models.entities import AgentRun, CostEvent, Evidence, Opportunity, Task
from app.orchestration.claude_code_adapter import WorkerResult, sanitize_text
from app.research.run_researcher import (
    CLAIM_TYPES,
    DEFAULT_RESEARCH_TOOLS,
    MAX_BUDGET_USD,
    SOURCE_RELIABILITIES,
    STANCES,
    ResearchPayloadError,
    _build_research_prompt,
    _compute_independently_confirmed,
    _resolve_duplicate_refs,
    build_research_argv,
    dispatch_research,
    parse_research_payload,
)


# ===========================================================================
# Fixtures
# ===========================================================================

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
    defaults = dict(slug="reviewer-opp", title="Reviewer Opportunity", thesis="Some thesis.")
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


def create_opportunity_via_api(client, slug="dossier-opp"):
    return client.post(
        "/api/opportunities", json={"slug": slug, "title": "T", "thesis": "thesis text"},
    ).json()


def insert_evidence_direct(opportunity_id: int, **overrides) -> int:
    """Bypasses the researcher pipeline entirely -- direct DB write via the
    app's own session, same technique as BUILDER's test_opportunity_dossier.py.
    Used here specifically to construct DB states the researcher pipeline
    itself cannot produce (cross-opportunity duplicate links, legacy rows
    with null M3.2 fields) so the dossier endpoint's robustness against
    *any* stored state -- not just states the pipeline can create -- is what
    gets tested."""
    defaults = dict(
        opportunity_id=opportunity_id, claim="Demand is growing", evidence_type="market_data",
        source="example source", source_url="https://example.com", confidence=0.5,
    )
    defaults.update(overrides)
    db_gen = app.dependency_overrides[get_db_dep]()
    db = next(db_gen)
    try:
        row = Evidence(**defaults)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db_gen.close()


# ===========================================================================
# 1. Datamodel / evidence contract -- independent schema inspection
# ===========================================================================

def test_claim_type_stance_source_reliability_are_plain_strings_not_db_enums():
    """CLAUDE.md SS3 / brief SS3: these are supposed to be application-level
    vocabularies (extensible without a migration), never a DB-level Enum
    column. Inspect the actual mapped column type, not just behavior."""
    from sqlalchemy import String
    cols = {c.name: c for c in Evidence.__table__.columns}
    assert isinstance(cols["claim_type"].type, String)
    assert isinstance(cols["stance"].type, String)
    assert isinstance(cols["source_reliability"].type, String)
    assert cols["claim_type"].nullable and cols["stance"].nullable and cols["source_reliability"].nullable


def test_duplicate_of_evidence_id_is_nullable_self_fk_set_null_indexed():
    cols = {c.name: c for c in Evidence.__table__.columns}
    col = cols["duplicate_of_evidence_id"]
    assert col.nullable
    assert col.index is True
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == "evidence"
    assert fks[0].ondelete == "SET NULL"


def test_found_at_is_distinct_column_from_created_at():
    cols = {c.name: c for c in Evidence.__table__.columns}
    assert "found_at" in cols and "created_at" in cols
    assert cols["found_at"].nullable is True


def test_opportunity_research_summary_is_nullable_text():
    from sqlalchemy import Text
    col = Opportunity.__table__.columns["research_summary"]
    assert col.nullable is True
    assert isinstance(col.type, Text)


def test_evidence_confidence_column_now_nullable_per_lead_finding1_decision():
    """Superseded by LEAD's decision on this file's own MEDIUM finding
    (confidence-honesty gap): at review time confidence was non-nullable
    Float, default 0.5, and this test asserted that as "unchanged by
    M3.2". LEAD's follow-up decision (Alembic revision a943ce8ca51f) made
    it nullable with no fallback default, specifically so "not assessed"
    can be a real NULL instead of a fabricated 0.5 -- inverted here rather
    than left asserting the now-superseded behavior."""
    from sqlalchemy import Float
    col = Evidence.__table__.columns["confidence"]
    assert isinstance(col.type, Float)
    assert col.nullable is True
    assert col.default is None


def test_costevent_and_task_schema_untouched_by_m3_2():
    task_cols = set(Task.__table__.columns.keys())
    cost_cols = set(CostEvent.__table__.columns.keys())
    # No M3.2 evidence/research vocabulary should have leaked into either.
    assert not (task_cols & {"claim_type", "stance", "source_reliability", "research_summary"})
    assert not (cost_cols & {"claim_type", "stance", "source_reliability", "research_summary"})


# ===========================================================================
# 2. Dossier endpoint -- adversarial
# ===========================================================================

def test_dossier_404_unknown_opportunity_independent(client):
    resp = client.get("/api/opportunities/999999/dossier")
    assert resp.status_code == 404


def test_dossier_endpoint_is_read_only_no_side_effects(client):
    """GET must never mutate Evidence/Opportunity state -- call it twice and
    diff the raw rows."""
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(opp["id"], claim="X", stance="SUPPORTS")
    first = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    second = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert first == second


def test_case_and_whitespace_claim_variants_group_together(client):
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(opp["id"], claim="Demand   is  growing", stance="SUPPORTS")
    insert_evidence_direct(opp["id"], claim="  demand IS Growing  ", stance="SUPPORTS")
    dossier = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert len(dossier["claims"]) == 1
    assert len(dossier["claims"][0]["supports"]) == 2


def test_empty_claim_does_not_crash_dossier_grouping(client):
    """The researcher pipeline itself always rejects an empty claim before
    persistence (parse_research_payload) -- but the dossier endpoint reads
    whatever Evidence rows actually exist, from any source, and must not
    assume that invariant. A legacy/manually-inserted empty-string claim
    must not 500."""
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(opp["id"], claim="", stance=None)
    resp = client.get(f"/api/opportunities/{opp['id']}/dossier")
    assert resp.status_code == 200
    assert len(resp.json()["claims"]) == 1


def test_duplicate_chain_a_b_c_only_root_counts_toward_independent_confirmations(client):
    """A <- B <- C: B duplicates A, C duplicates B. Only A (the sole row
    with duplicate_of_evidence_id IS NULL) should count."""
    opp = create_opportunity_via_api(client)
    a = insert_evidence_direct(opp["id"], claim="Same claim", stance="SUPPORTS")
    b = insert_evidence_direct(opp["id"], claim="Same claim", stance="SUPPORTS", duplicate_of_evidence_id=a)
    insert_evidence_direct(opp["id"], claim="Same claim", stance="SUPPORTS", duplicate_of_evidence_id=b)
    dossier = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    group = dossier["claims"][0]
    assert group["independent_confirmations"] == 1
    assert len(group["supports"]) == 3  # all three still visible, chain preserved


def test_duplicate_link_to_a_DIFFERENT_opportunity_does_not_crash_dossier(client):
    """FINDING (LOW, defense-in-depth): duplicate_of_evidence_id is a bare
    self-FK on evidence.id with no application- or DB-level constraint that
    the referenced row belongs to the SAME opportunity_id. The researcher
    pipeline's own _resolve_duplicate_refs is batch-scoped (one dispatch,
    one opportunity) and cannot produce this today, but nothing in the
    schema prevents a future write path from doing so, and the dossier
    endpoint has no defense of its own -- it renders whatever is stored.
    This proves the endpoint at least fails safe (no crash, no fabricated
    cross-opportunity data merged into the wrong dossier), not that the gap
    is closed."""
    opp_a = create_opportunity_via_api(client, slug="opp-a")
    opp_b = create_opportunity_via_api(client, slug="opp-b")
    other_id = insert_evidence_direct(opp_b["id"], claim="Unrelated claim in B")
    same_opp_row = insert_evidence_direct(
        opp_a["id"], claim="Claim in A", duplicate_of_evidence_id=other_id,
    )
    resp = client.get(f"/api/opportunities/{opp_a['id']}/dossier")
    assert resp.status_code == 200
    dossier = resp.json()
    assert len(dossier["claims"]) == 1  # opp_b's evidence never leaks into opp_a's dossier
    item = dossier["claims"][0]["supports"] + dossier["claims"][0]["unspecified"]
    assert item[0]["evidence_id"] == same_opp_row
    assert item[0]["duplicate_of_evidence_id"] == other_id  # stored verbatim, not validated


def test_inconsistent_stance_same_normalized_claim_both_lists_populated(client):
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(opp["id"], claim="Market is underserved", stance="SUPPORTS")
    insert_evidence_direct(opp["id"], claim="market is underserved", stance="CONTRADICTS")
    dossier = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    group = dossier["claims"][0]
    assert len(group["supports"]) == 1 and len(group["contradicts"]) == 1
    # Both count toward independent_confirmations (it groups by claim only,
    # not by claim+stance) -- documents the honesty caveat already in the
    # Field description is load-bearing, not decorative.
    assert group["independent_confirmations"] == 2


def test_legacy_evidence_with_null_m3_2_fields_still_visible_and_unspecified(client):
    """Pre-M3.2 Evidence rows (claim_type/stance/source_reliability/found_at
    all NULL, since the migration adds them as nullable) must remain fully
    visible in the dossier, not silently dropped."""
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(
        opp["id"], claim="Legacy row", stance=None, claim_type=None,
        source_reliability=None, found_at=None,
    )
    dossier = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    group = dossier["claims"][0]
    assert len(group["unspecified"]) == 1
    item = group["unspecified"][0]
    assert item["claim_type"] is None and item["source_reliability"] is None and item["found_at"] is None
    assert group["independent_confirmations"] == 1  # still counts (duplicate_of is NULL)


def test_independent_confirmations_field_description_carries_the_honesty_caveat():
    """The brief explicitly requires the API not claim proven source
    independence. Verify the caveat is actually attached to the OpenAPI
    schema field (reaches API consumers/docs), not just a code comment."""
    from app.api.routes import DossierClaimGroup
    schema = DossierClaimGroup.model_json_schema()
    description = schema["properties"]["independent_confirmations"]["description"]
    assert "NOT a measure of proven source independence" in description


# ===========================================================================
# 3. Researcher CLI / security -- independently rebuilt argv assertions
# ===========================================================================

def test_researcher_argv_exact_required_flags_present_and_ordered_pairs():
    argv = build_research_argv(prompt="investigate X")
    assert argv[0] == "claude"
    assert argv[argv.index("-p") + 1] == "investigate X"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-budget-usd") + 1] == MAX_BUDGET_USD
    assert "--safe-mode" in argv
    tools_idx = argv.index("--allowedTools")
    assert argv[tools_idx + 1 : tools_idx + 1 + len(DEFAULT_RESEARCH_TOOLS)] == list(DEFAULT_RESEARCH_TOOLS)


@pytest.mark.parametrize("forbidden", ["Edit", "Write", "Bash", "Bash(pytest *)", "Bash(git *)"])
def test_researcher_forbidden_tools_rejected(forbidden):
    with pytest.raises(ValueError):
        build_research_argv(prompt="x", allowed_tools=(forbidden,))


@pytest.mark.parametrize("banned_flag", ["--bare", "--continue", "--resume", "--worktree", "--dangerously-skip-permissions"])
def test_researcher_argv_never_contains_dangerous_flags(banned_flag):
    argv = build_research_argv(prompt="investigate X")
    assert banned_flag not in argv


def test_researcher_argv_is_a_list_shell_false_at_call_site():
    """Confirms run_researcher's actual subprocess.run call, not just the
    argv builder in isolation -- shell=True would make argv-list-safety moot.

    NOTE: intentionally uses sys.modules + getattr on the *function*, not
    `import app.research.run_researcher as mod` -- app/research/__init__.py
    re-exports a symbol named identically to the submodule
    (`from app.research.run_researcher import run_researcher`), which
    rebinds the `run_researcher` attribute on the `app.research` package to
    the function once __init__.py has executed. `import ... as mod` resolves
    through that attribute chain, not sys.modules, so it silently returns
    the function instead of the module -- a Python import-shadowing quirk of
    the package layout, not a bug in run_researcher itself.
    """
    import sys
    import inspect
    import app.research.run_researcher  # noqa: F401 -- ensure it's imported
    mod = sys.modules["app.research.run_researcher"]
    src = inspect.getsource(mod.run_researcher)
    assert "shell=True" not in src
    assert "shell=" not in src  # never explicitly set at all -> defaults to False


def test_max_budget_usd_has_no_function_parameter_anywhere_in_call_chain():
    """Independent of BUILDER's own test with the same intent: inspect the
    real function signatures directly rather than trusting behavior alone."""
    import inspect
    from app.research.run_researcher import run_researcher, dispatch_research
    for fn in (build_research_argv, run_researcher, dispatch_research):
        params = inspect.signature(fn).parameters
        assert not any("budget" in p.lower() for p in params), f"{fn.__name__} exposes a budget parameter"


def test_max_budget_usd_constant_is_a_string_matching_dollar_format_le_2():
    assert MAX_BUDGET_USD == "2.00"
    assert float(MAX_BUDGET_USD) <= 2.00


def test_dispatch_research_makes_exactly_one_run_researcher_call_even_on_failure(db_session):
    opp = _make_opportunity(db_session)
    calls = {"n": 0}

    def failing(**kw):
        calls["n"] += 1
        return WorkerResult(
            ok=False, exit_code=1, session_id=None, result_text=None, usage={},
            total_cost_usd=None, is_error=True, error_kind="nonzero_exit",
            error_detail="boom", stderr_excerpt=None,
        )

    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=failing)
    assert calls["n"] == 1  # no retry after a real model-call failure


# ===========================================================================
# 4. Hallucination defense: technical enforcement vs prompt-only policy
# ===========================================================================
# This section deliberately proves the NEGATIVE for several of the brief's
# adversarial scenarios: the prompt (SS6 of this file's own
# _build_research_prompt) asks the model not to do these things, but
# parse_research_payload/dispatch_research apply NO technical check against
# any of them. That gap is the actual finding -- these are not bugs to fix
# (an LLM output-critic is explicitly out of M3.2's scope), but M3.2 must
# not be represented as technically preventing what it only asks nicely for.

def test_FINDING_fake_url_labeled_FACT_is_accepted_with_no_verification(db_session):
    opp = _make_opportunity(db_session)
    evidence = [{
        "id": "e1", "claim": "The market is worth $50B", "claim_type": "FACT",
        "stance": "SUPPORTS", "source": "Totally Fake Journal",
        "source_url": "https://this-domain-does-not-exist-fabricated.invalid/article",
        "source_reliability": "HIGH", "confidence": 0.95,
    }]
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(evidence),
    )
    assert run.success is True
    row = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).one()
    assert row.claim_type == "FACT"  # accepted verbatim -- no URL reachability/format check exists
    assert row.source_url.endswith(".invalid/article")


def test_FINDING_every_claim_marked_FACT_with_zero_counter_evidence_raises_no_anomaly(db_session):
    """Confirmation-bias-only output (every entry FACT/SUPPORTS, no
    CONTRADICTS at all) is technically indistinguishable from genuinely
    balanced research -- no anomaly, no rejection, no lowered confidence."""
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": f"e{i}", "claim": f"Claim {i}", "claim_type": "FACT", "stance": "SUPPORTS",
         "source": f"src{i}", "confidence": 0.9}
        for i in range(5)
    ]
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(evidence),
    )
    assert run.success is True
    assert "anomal" not in run.output_summary.lower().replace("anomalies=0", "")
    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
    assert all(r.claim_type == "FACT" and r.stance == "SUPPORTS" for r in rows)


def test_FINDING_research_summary_missing_every_required_red_team_section_still_persists(db_session):
    """parse_research_payload's only check on research_summary is
    "non-empty string" (SS parse_research_payload). The eight mandatory
    sections the prompt lists (problem/pain, affected, supporting evidence,
    counter-evidence, unknowns, competition, market gap, "why NOT to test")
    are enforced by the prompt text alone -- nothing downstream verifies
    their presence."""
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(
            [{"id": "e1", "claim": "x", "source": "s"}], research_summary="ok",
        ),
    )
    assert run.success is True
    refreshed = db_session.get(Opportunity, opp.id)
    assert refreshed.research_summary == "ok"  # zero required sections, persisted anyway


def test_FINDING_a_smuggled_TEST_recommendation_in_research_summary_is_not_stripped_or_rejected(db_session):
    """The prompt explicitly forbids a TEST/WATCH/REJECT recommendation.
    Nothing technically enforces that: a research_summary that includes one
    anyway is persisted verbatim, word for word."""
    opp = _make_opportunity(db_session)
    smuggled = "Recommendation: TEST this immediately, high confidence GO."
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(
            [{"id": "e1", "claim": "x", "source": "s"}], research_summary=smuggled,
        ),
    )
    assert run.success is True
    refreshed = db_session.get(Opportunity, opp.id)
    assert refreshed.research_summary == smuggled


def test_FINDING_no_counter_evidence_found_is_not_technically_distinguishable_from_not_searched(db_session):
    """The brief demands "geen tegenbewijs gevonden -> moet expliciet worden
    gemeld, niet stilzwijgend worden overgeslagen". There is no structured
    field or check for this -- zero CONTRADICTS rows produces exactly the
    same persisted state whether the researcher genuinely searched and
    found none, or never looked. Confirms this is a prompt-only ask."""
    opp = _make_opportunity(db_session)
    run = dispatch_research(
        db_session, opp.id, repo_path="/fake",
        run_researcher_fn=lambda **kw: _ok_worker_result(
            [{"id": "e1", "claim": "x", "stance": "SUPPORTS", "source": "s"}],
            research_summary="All good, no issues found anywhere.",
        ),
    )
    assert run.success is True
    contradicts = [
        r for r in db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
        if r.stance == "CONTRADICTS"
    ]
    assert contradicts == []  # indistinguishable from "didn't look"


def test_prompt_text_does_forbid_all_of_the_above_even_though_nothing_enforces_it():
    """Confirms the prompt-policy side genuinely exists (so the findings
    above are "enforcement gap", not "nobody even asked")."""
    opp = Opportunity(slug="x", title="T", thesis="Thesis")
    prompt = _build_research_prompt(opp)
    assert "never invent a source" in prompt
    assert "Do not include a TEST/WATCH/REJECT recommendation" in prompt
    assert "Actively search for counter-evidence" in prompt
    assert "never label a claim fact unless" in prompt.lower()


# ===========================================================================
# 5. JSON / parsing -- adversarial
# ===========================================================================

def test_malformed_outer_json_is_structured_failure_not_exception():
    from app.research.run_researcher import run_researcher
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout="{not json", stderr="")
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.ok is False
    assert result.error_kind == "invalid_json"


def test_malformed_inner_json_raises_ResearchPayloadError_not_silent_fabrication():
    with pytest.raises(ResearchPayloadError):
        parse_research_payload("this is not json at all { [ }")


def test_missing_top_level_evidence_key_raises():
    with pytest.raises(ResearchPayloadError):
        parse_research_payload(json.dumps({"research_summary": "s"}))


def test_missing_research_summary_key_raises():
    with pytest.raises(ResearchPayloadError):
        parse_research_payload(json.dumps({"evidence": []}))


def test_non_list_evidence_raises():
    with pytest.raises(ResearchPayloadError):
        parse_research_payload(json.dumps({"evidence": {"id": "e1"}, "research_summary": "s"}))


@pytest.mark.parametrize("field,bad_value", [
    ("claim_type", "DEFINITELY_TRUE"), ("stance", "PROVES"), ("source_reliability", "VERY_HIGH"),
])
def test_invalid_enum_value_becomes_null_or_UNKNOWN_never_raises(field, bad_value):
    entry = {"id": "e1", "claim": "c", "source": "s", field: bad_value}
    payload = parse_research_payload(json.dumps({"evidence": [entry], "research_summary": "s"}))
    assert len(payload.entries) == 1
    e = payload.entries[0]
    if field == "source_reliability":
        assert e.source_reliability == "UNKNOWN"
    else:
        assert getattr(e, field) is None
    assert any(bad_value in a for a in payload.anomalies)


def test_null_source_url_stays_null_never_fabricated():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "source_url": None}],
        "research_summary": "s",
    }))
    assert payload.entries[0].source_url is None


def test_empty_string_source_url_normalized_to_null_not_stored_as_empty():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "source_url": "   "}],
        "research_summary": "s",
    }))
    assert payload.entries[0].source_url is None


def test_oversized_claim_source_and_summary_are_truncated_not_rejected():
    huge_claim = "A" * 10000
    huge_summary = "B" * 50000
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": huge_claim, "source": "s" * 2000}],
        "research_summary": huge_summary,
    }))
    assert len(payload.entries[0].claim) <= 4000 + len("...[truncated]")
    assert len(payload.entries[0].source) <= 500 + len("...[truncated]")
    assert len(payload.research_summary) <= 20000 + len("...[truncated]")


def test_unexpected_top_level_and_per_entry_keys_are_ignored_not_fabricated_into_fields():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "debug_internal_notes": "should be dropped"}],
        "research_summary": "s", "unexpected_top_level_key": "also dropped",
    }))
    assert len(payload.entries) == 1
    assert not hasattr(payload.entries[0], "debug_internal_notes")


def test_evidence_entry_that_is_not_an_object_is_skipped_with_anomaly_not_crash():
    payload = parse_research_payload(json.dumps({
        "evidence": ["just a string", 42, None, {"id": "e1", "claim": "c", "source": "s"}],
        "research_summary": "s",
    }))
    assert len(payload.entries) == 1
    skip_anomalies = [a for a in payload.anomalies if "was not a JSON object" in a]
    assert len(skip_anomalies) == 3


def test_duplicate_temp_id_second_occurrence_becomes_unreferenceable(db_session):
    """Two entries both claim id="e1". The first wins the id; the second is
    left unreferenceable (temp_id=None) rather than silently overwriting or
    letting a later duplicate_of="e1" ambiguously resolve to either row."""
    payload = parse_research_payload(json.dumps({
        "evidence": [
            {"id": "e1", "claim": "first", "source": "s1"},
            {"id": "e1", "claim": "second", "source": "s2"},
        ],
        "research_summary": "s",
    }))
    assert payload.entries[0].temp_id == "e1"
    assert payload.entries[1].temp_id is None
    assert any("duplicate or invalid id" in a for a in payload.anomalies)


def test_forward_duplicate_ref_to_a_later_entry_resolves_correctly(db_session):
    """e1 (index 0) declares duplicate_of="e2", where e2 is defined LATER in
    the array (index 1). _resolve_duplicate_refs indexes all known temp_ids
    up front, so this is NOT treated as an unknown ref -- confirms forward
    references work end-to-end through real persistence, not just at the
    resolver-function level."""
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "Same claim", "source": "a", "stance": "SUPPORTS", "duplicate_of": "e2"},
        {"id": "e2", "claim": "Same claim", "source": "b", "stance": "SUPPORTS"},
    ]
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))
    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id).order_by(Evidence.id)).all()
    e1, e2 = rows
    assert e1.duplicate_of_evidence_id == e2.id


def test_self_referencing_duplicate_of_resolves_to_null_with_anomaly():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "duplicate_of": "e1"}],
        "research_summary": "s",
    }))
    dup_idx = _resolve_duplicate_refs(payload.entries)
    assert dup_idx[0] is None
    assert any("unknown/self id" in a for a in payload.entries[0].anomalies)


def test_unknown_duplicate_ref_resolves_to_null_with_anomaly_isolated_unit_level():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "duplicate_of": "ghost-id"}],
        "research_summary": "s",
    }))
    dup_idx = _resolve_duplicate_refs(payload.entries)
    assert dup_idx[0] is None


# ===========================================================================
# 6. Provenance / duplicates -- best-effort caveat
# ===========================================================================

def test_FINDING_two_articles_citing_the_same_primary_source_both_count_as_independent(db_session):
    """CRITICAL caveat the brief demands be surfaced explicitly: the system
    has NO mechanism to detect that two different source_urls actually cite
    the same primary source -- it only trusts the model's own duplicate_of
    self-report. Two entries with different sources/URLs and no duplicate_of
    link, that a human would recognize as re-reporting one underlying study,
    are counted as two fully independent confirmations."""
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "TAM is $2B", "stance": "SUPPORTS", "source": "TechCrunch",
         "source_url": "https://techcrunch.com/article-citing-study-x"},
        {"id": "e2", "claim": "TAM is $2B", "stance": "SUPPORTS", "source": "VentureBeat",
         "source_url": "https://venturebeat.com/different-article-citing-same-study-x"},
        # no duplicate_of set by the model -- nothing forces it to notice
    ]
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))
    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()
    assert all(r.independently_confirmed for r in rows)  # best-effort, not proven-independent
    assert all(r.duplicate_of_evidence_id is None for r in rows)


def test_two_of_three_sources_duplicate_only_the_two_genuinely_independent_confirm(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "c", "stance": "SUPPORTS", "source": "a"},
        {"id": "e2", "claim": "c", "stance": "SUPPORTS", "source": "b", "duplicate_of": "e1"},
        {"id": "e3", "claim": "c", "stance": "SUPPORTS", "source": "d"},
    ]
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))
    rows = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id).order_by(Evidence.id)).all()
    e1, e2, e3 = rows
    assert e1.independently_confirmed is True
    assert e2.independently_confirmed is False  # duplicate, excluded
    assert e3.independently_confirmed is True


def test_case_whitespace_variant_claims_still_confirm_each_other_at_computation_level():
    payload = parse_research_payload(json.dumps({
        "evidence": [
            {"id": "e1", "claim": "Demand is  growing", "stance": "SUPPORTS", "source": "a"},
            {"id": "e2", "claim": "  demand IS growing", "stance": "SUPPORTS", "source": "b"},
        ],
        "research_summary": "s",
    }))
    dup_idx = _resolve_duplicate_refs(payload.entries)
    confirmed = _compute_independently_confirmed(payload.entries, dup_idx)
    assert confirmed == [True, True]


# ===========================================================================
# 7. Confidence / UNKNOWN honesty
# ===========================================================================

def test_missing_confidence_becomes_null_and_is_recorded_as_anomaly():
    """Superseded by LEAD's finding-1 decision: confidence is now nullable
    (Alembic revision a943ce8ca51f), so a declined confidence is a real
    NULL, not a 0.5 fallback."""
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s"}],  # no confidence key at all
        "research_summary": "s",
    }))
    assert payload.entries[0].confidence is None
    assert any("declined to estimate" in a for a in payload.entries[0].anomalies)


def test_FIXED_dossier_response_now_distinguishes_genuine_0_5_from_declined_null(db_session):
    """Regression guard for this file's own MEDIUM finding
    (test_FINDING_dossier_response_cannot_distinguish_genuine_0_5_from_fallback_0_5,
    as originally reported): LEAD's decision made Evidence.confidence
    nullable (Alembic revision a943ce8ca51f) with no fallback value, so a
    genuinely-estimated 0.5 and a declined-to-estimate row are no longer
    schema-identical -- the second is now a real NULL. Inverted, not
    deleted, so a future regression back to a fabricated fallback value
    would be caught here."""
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "genuinely medium confidence", "source": "s", "confidence": 0.5},
        {"id": "e2", "claim": "declined to estimate", "source": "s"},  # no confidence key
    ]
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))
    rows = {r.claim: r for r in db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all()}
    assert rows["genuinely medium confidence"].confidence == 0.5
    assert rows["declined to estimate"].confidence is None
    assert "declined to estimate" in run.output_summary


def test_UNKNOWN_claim_type_and_source_reliability_survive_end_to_end_to_dossier(client):
    opp = create_opportunity_via_api(client)
    insert_evidence_direct(opp["id"], claim="c", claim_type="UNKNOWN", source_reliability="UNKNOWN")
    dossier = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    item = dossier["claims"][0]["unspecified"][0]
    assert item["claim_type"] == "UNKNOWN"
    assert item["source_reliability"] == "UNKNOWN"


def test_out_of_range_confidence_falls_back_to_null_with_anomaly_not_clamped_silently():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "confidence": 5.0}],
        "research_summary": "s",
    }))
    assert payload.entries[0].confidence is None
    assert any("out of range" in a for a in payload.entries[0].anomalies)


def test_confidence_as_bool_is_rejected_not_silently_coerced_to_1_or_0():
    payload = parse_research_payload(json.dumps({
        "evidence": [{"id": "e1", "claim": "c", "source": "s", "confidence": True}],
        "research_summary": "s",
    }))
    assert payload.entries[0].confidence is None
    assert any("must be numeric, got bool" in a for a in payload.entries[0].anomalies)


# ===========================================================================
# 8. Database atomicity
# ===========================================================================

def test_flush_failure_leaves_no_evidence_and_no_summary(db_session):
    """Only the FIRST flush (the evidence_objs flush inside dispatch_research's
    own try block) is made to fail -- commit() internally calls flush() too,
    so failing every flush call would also break the fallback
    _log_agent_run's own commit, conflating this test with the
    double-failure scenario covered separately below."""
    opp = _make_opportunity(db_session)
    evidence = [{"id": "e1", "claim": "a", "source": "s"}]

    original_flush = SASession.flush
    call_count = {"n": 0}

    def failing_first_flush(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated flush failure")
        return original_flush(self, *a, **kw)

    with patch.object(SASession, "flush", failing_first_flush):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))

    assert run.success is False
    db_session.expire_all()
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []
    assert db_session.get(Opportunity, opp.id).research_summary is None


def test_commit_failure_after_duplicate_link_resolution_rolls_back_everything(db_session):
    opp = _make_opportunity(db_session)
    evidence = [
        {"id": "e1", "claim": "a", "source": "s1", "stance": "SUPPORTS"},
        {"id": "e2", "claim": "a", "source": "s2", "stance": "SUPPORTS", "duplicate_of": "e1"},
    ]
    original_commit = SASession.commit
    call_count = {"n": 0}

    def failing_commit(self, *a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated commit failure post-duplicate-resolution")
        return original_commit(self, *a, **kw)

    with patch.object(SASession, "commit", failing_commit):
        run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))

    assert run.success is False
    db_session.expire_all()
    assert db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).all() == []


def test_FINDING_agentrun_double_failure_can_leave_zero_agentrun_rows(db_session):
    """Edge case the brief explicitly asks be checked (SS11: 'of precies een
    AgentRun per dispatch ook bij failure daadwerkelijk gegarandeerd is').
    dispatch_research's three failure branches all call _log_agent_run
    directly with no try/except of their own. If the DB fails BOTH the main
    write and that fallback logging commit (a genuine double failure -- the
    kind a total DB outage would cause), the guarantee breaks: zero AgentRun
    rows survive, and the exception propagates uncaught out of
    dispatch_research. Reported as a LOW-severity gap (only reachable under
    an already-unrecoverable DB outage, not a normal failure mode) rather
    than fixed -- restructuring the failure-logging path into its own
    fully-independent transaction is arguably more machinery than this
    edge case's real-world impact justifies for M3.2 (CLAUDE.md SS15), but
    LEAD should make the call."""
    opp = _make_opportunity(db_session)
    evidence = [{"id": "e1", "claim": "a", "source": "s"}]

    def always_failing_commit(self, *a, **kw):
        raise RuntimeError("simulated total DB outage")

    with patch.object(SASession, "commit", always_failing_commit):
        with pytest.raises(RuntimeError):
            dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence))

    db_session.rollback()
    assert db_session.scalars(select(AgentRun)).all() == []


# ===========================================================================
# 9. AgentRun / cost / secret sanitization
# ===========================================================================

def test_exactly_one_agentrun_row_on_success(db_session):
    opp = _make_opportunity(db_session)
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result([{"id": "e1", "claim": "a", "source": "s"}]))
    assert len(db_session.scalars(select(AgentRun)).all()) == 1


def test_cost_eur_always_0_0_and_no_costevent_written(db_session):
    opp = _make_opportunity(db_session)
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result([{"id": "e1", "claim": "a", "source": "s"}], total_cost_usd=1.99))
    assert run.cost_eur == 0.0
    assert db_session.scalars(select(CostEvent)).all() == []
    assert "1.99" in run.output_summary  # USD estimate lands only in text


@pytest.mark.parametrize("label,secret", [
    ("anthropic_key", "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET999999"),
    ("openai_key", "sk-FAKEOPENAIKEYFAKEOPENAIKEYFAKEOPENAIKEY12"),
    ("bearer", "Bearer FAKEBEARERTOKENVALUE1234567890ABCDEFGH"),
    ("password_eq", "password=SuperSecretFake123!"),
    ("token_eq", "token=totally-fake-token-value-abcdef"),
    ("aws_like", "AKIAFAKEACCESSKEYFAKE"),
])
def test_nepsecret_never_survives_in_any_persisted_evidence_field(db_session, label, secret):
    opp = _make_opportunity(db_session)
    evidence = [{
        "id": "e1", "claim": f"claim mentioning {secret}", "source": f"source with {secret}",
        "source_url": f"https://example.com/{secret}",
    }]
    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result(evidence, research_summary=f"summary with {secret}"))
    row = db_session.scalars(select(Evidence).where(Evidence.opportunity_id == opp.id)).one()
    opp_refreshed = db_session.get(Opportunity, opp.id)
    for field_val in (row.claim, row.source, row.source_url or "", opp_refreshed.research_summary):
        assert secret not in field_val


@pytest.mark.parametrize("label,secret", [
    ("anthropic_key", "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET999999"),
    ("bearer", "Bearer FAKEBEARERTOKENVALUE1234567890ABCDEFGH"),
])
def test_nepsecret_in_worker_failure_detail_never_reaches_agentrun(db_session, label, secret):
    opp = _make_opportunity(db_session)
    fail_result = WorkerResult(
        ok=False, exit_code=1, session_id=None, result_text=None, usage={},
        total_cost_usd=None, is_error=True, error_kind="nonzero_exit",
        error_detail=f"process failed, leaked env: {secret}", stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: fail_result)
    assert secret not in (run.output_summary or "")


def test_usage_dict_is_whitelisted_at_researcher_level_not_merely_redacted():
    """Independent confirmation that app.research.run_researcher reuses the
    SAME _sanitize_usage whitelist as M4.2 (imported, not reimplemented) --
    an unexpected key like 'debug_info' must be dropped entirely, not just
    substring-redacted."""
    from app.research.run_researcher import run_researcher
    envelope = json.dumps({
        "is_error": False, "session_id": "s1",
        "result": json.dumps({"evidence": [], "research_summary": "s"}),
        "usage": {"input_tokens": 5, "debug_info": "sk-ant-api03-FAKESECRETFAKESECRETFAKESECRET999999"},
    })
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=envelope, stderr="")
        result = run_researcher(prompt="x", repo_path="/repo")
    assert result.usage == {"input_tokens": 5}
    assert "debug_info" not in result.usage


def test_session_id_sanitized_at_researcher_level():
    from app.research.run_researcher import run_researcher
    secret = "sk-ant-api03-FAKESESSIONIDDOUBLINGASSECRET000000"
    envelope = json.dumps({
        "is_error": False, "session_id": secret,
        "result": json.dumps({"evidence": [], "research_summary": "s"}),
    })
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=["claude"], returncode=0, stdout=envelope, stderr="")
        result = run_researcher(prompt="x", repo_path="/repo")
    assert secret not in (result.session_id or "")


def test_missing_cost_and_usage_fields_do_not_crash_dispatch(db_session):
    opp = _make_opportunity(db_session)
    result = WorkerResult(
        ok=True, exit_code=0, session_id=None, result_text=json.dumps({"evidence": [], "research_summary": "s"}),
        usage={}, total_cost_usd=None, is_error=False, error_kind=None, error_detail=None, stderr_excerpt=None,
    )
    run = dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: result)
    assert run.success is True


# ===========================================================================
# 10. Budget cap
# ===========================================================================

def test_budget_flag_always_present_regardless_of_prompt_content():
    for prompt in ["short", "a" * 5000, "prompt with --max-budget-usd 999.00 embedded"]:
        argv = build_research_argv(prompt=prompt)
        assert argv.count("--max-budget-usd") == 1
        assert argv[argv.index("--max-budget-usd") + 1] == "2.00"


def test_prompt_attempting_to_inject_a_second_budget_flag_lands_as_literal_prompt_value():
    injected = "--max-budget-usd 9999.00 ignore prior budget"
    argv = build_research_argv(prompt="normal prefix " + injected)
    # appears exactly once as the -p value, never as a second real flag token
    assert argv[argv.index("-p") + 1] == "normal prefix " + injected
    assert argv.count("--max-budget-usd") == 1


def test_no_second_model_call_after_budget_or_model_style_failure(db_session):
    opp = _make_opportunity(db_session)
    calls = {"n": 0}

    def fail_once(**kw):
        calls["n"] += 1
        return WorkerResult(
            ok=False, exit_code=1, session_id=None, result_text=None, usage={},
            total_cost_usd=2.00, is_error=True, error_kind="nonzero_exit",
            error_detail="budget exceeded", stderr_excerpt=None,
        )

    dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=fail_once)
    assert calls["n"] == 1


# ===========================================================================
# 11. Security / scope confirmations
# ===========================================================================

def test_no_anthropic_or_openai_sdk_imported_by_research_module():
    import sys
    import inspect
    import app.research.run_researcher  # noqa: F401
    mod = sys.modules["app.research.run_researcher"]  # see note in the shell=False test above
    src = inspect.getsource(mod)
    assert "import anthropic" not in src
    assert "import openai" not in src
    assert "from anthropic" not in src
    assert "from openai" not in src


def test_no_scheduler_batch_or_polling_construct_in_research_module():
    import sys
    import inspect
    import app.research.run_researcher  # noqa: F401
    mod = sys.modules["app.research.run_researcher"]  # see note in the shell=False test above
    src = inspect.getsource(mod)
    for token in ("APScheduler", "while True", "celery", "cron"):
        assert token not in src


def test_rerun_is_refused_not_silently_overwritten(db_session):
    from app.research.run_researcher import ResearchAlreadyExistsError
    opp = _make_opportunity(db_session, research_summary="already researched")
    with pytest.raises(ResearchAlreadyExistsError):
        dispatch_research(db_session, opp.id, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result([]))


def test_unknown_opportunity_id_raises_not_found_not_silent_noop(db_session):
    from app.research.run_researcher import OpportunityNotFoundError
    with pytest.raises(OpportunityNotFoundError):
        dispatch_research(db_session, 999999, repo_path="/fake", run_researcher_fn=lambda **kw: _ok_worker_result([]))


def test_no_http_endpoint_currently_triggers_dispatch_research():
    """Confirms dispatch_research is not wired to any route yet in M3.2 v1
    -- the only way to invoke it today is a direct call (e.g. a future CLI
    for the live dogfood run), matching 'geen live Researcher-run
    uitgevoerd' in the known status. If this ever changes, this test will
    force a REVIEWER look at the new endpoint's own security posture."""
    import inspect
    import app.api.routes as routes_mod
    src = inspect.getsource(routes_mod)
    assert "dispatch_research" not in src


def test_evidence_type_enum_values_did_not_change_shape():
    assert CLAIM_TYPES == frozenset({"FACT", "INFERENCE", "ESTIMATE", "UNKNOWN"})
    assert STANCES == frozenset({"SUPPORTS", "CONTRADICTS"})
    assert SOURCE_RELIABILITIES == frozenset({"HIGH", "MEDIUM", "LOW", "UNKNOWN"})
