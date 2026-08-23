"""M3.2 BUILDER: read-only opportunity dossier endpoint.

Scope: GET /api/opportunities/{id}/dossier only -- groups Evidence rows by
(normalized) claim text, splits each group into SUPPORTS/CONTRADICTS (stance
is always relative to that row's own `claim`, never to Opportunity.thesis),
and counts "independent confirmations" as rows with
duplicate_of_evidence_id IS NULL. No LLM call, no write path, no Task/
ClaudeCodeAdapter involvement -- that is out of scope for this endpoint.
"""

from datetime import datetime

from app.db.session import get_db as get_db_dep
from app.main import app
from app.models.entities import Evidence


def create_opportunity(client, slug="dossier-opp"):
    return client.post(
        "/api/opportunities",
        json={"slug": slug, "title": "Dossier Test Opp", "thesis": "thesis text"},
    ).json()


def add_evidence(opportunity_id: int, **overrides) -> int:
    """Inserts an Evidence row directly via the app's own DB session
    (there is no POST /evidence endpoint -- that belongs to a future
    Researcher slice, out of scope here) and returns its id.
    """
    defaults = dict(
        opportunity_id=opportunity_id,
        claim="Demand is growing",
        evidence_type="market_data",
        source="example source",
        source_url="https://example.com",
        confidence=0.5,
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


def test_dossier_404_on_unknown_opportunity(client):
    response = client.get("/api/opportunities/999999/dossier")
    assert response.status_code == 404


def test_dossier_with_no_evidence(client):
    opp = create_opportunity(client)
    response = client.get(f"/api/opportunities/{opp['id']}/dossier")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == opp["id"]
    assert body["title"] == "Dossier Test Opp"
    assert body["thesis"] == "thesis text"
    assert body["status"] == "discovered"
    assert body["research_summary"] is None
    assert body["claims"] == []


def test_dossier_groups_by_normalized_claim_text(client):
    opp = create_opportunity(client)
    add_evidence(opp["id"], claim="Demand is growing", stance="SUPPORTS")
    add_evidence(opp["id"], claim="  demand IS   growing  ", stance="SUPPORTS")
    add_evidence(opp["id"], claim="Competition is weak", stance="SUPPORTS")

    body = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert len(body["claims"]) == 2

    demand_group = next(c for c in body["claims"] if c["claim"] == "Demand is growing")
    assert len(demand_group["supports"]) == 2

    competition_group = next(c for c in body["claims"] if c["claim"] == "Competition is weak")
    assert len(competition_group["supports"]) == 1


def test_dossier_separates_supports_and_contradicts(client):
    opp = create_opportunity(client)
    add_evidence(
        opp["id"],
        claim="Market is underserved",
        stance="SUPPORTS",
        source="source-a",
        claim_type="FACT",
        source_reliability="HIGH",
    )
    add_evidence(
        opp["id"],
        claim="Market is underserved",
        stance="CONTRADICTS",
        source="source-b",
        claim_type="ESTIMATE",
        source_reliability="LOW",
    )

    body = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert len(body["claims"]) == 1
    group = body["claims"][0]

    assert len(group["supports"]) == 1
    assert group["supports"][0]["source"] == "source-a"
    assert group["supports"][0]["claim_type"] == "FACT"
    assert group["supports"][0]["source_reliability"] == "HIGH"

    assert len(group["contradicts"]) == 1
    assert group["contradicts"][0]["source"] == "source-b"
    assert group["contradicts"][0]["claim_type"] == "ESTIMATE"
    assert group["contradicts"][0]["source_reliability"] == "LOW"

    assert group["unspecified"] == []


def test_dossier_evidence_without_stance_goes_to_unspecified(client):
    opp = create_opportunity(client)
    add_evidence(opp["id"], claim="Unclear signal", stance=None)

    body = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    group = body["claims"][0]
    assert group["supports"] == []
    assert group["contradicts"] == []
    assert len(group["unspecified"]) == 1


def test_dossier_independent_confirmation_count_excludes_duplicates(client):
    opp = create_opportunity(client)
    original_id = add_evidence(opp["id"], claim="Shared claim", stance="SUPPORTS", source="source-a")
    add_evidence(
        opp["id"],
        claim="Shared claim",
        stance="SUPPORTS",
        source="source-b",
        duplicate_of_evidence_id=original_id,
    )

    body = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert len(body["claims"]) == 1
    group = body["claims"][0]

    # Both rows are shown...
    assert len(group["supports"]) == 2
    # ...but only the non-duplicate row counts toward independent confirmation.
    assert group["independent_confirmations"] == 1

    duplicate_item = next(item for item in group["supports"] if item["source"] == "source-b")
    assert duplicate_item["duplicate_of_evidence_id"] == original_id
    original_item = next(item for item in group["supports"] if item["source"] == "source-a")
    assert original_item["duplicate_of_evidence_id"] is None


def test_dossier_includes_research_summary_and_thesis_relative_stance_fields(client):
    opp = create_opportunity(client)

    db_gen = app.dependency_overrides[get_db_dep]()
    db = next(db_gen)
    try:
        from app.models.entities import Opportunity

        row = db.get(Opportunity, opp["id"])
        row.research_summary = "Synthesis of the evidence so far."
        db.commit()
    finally:
        db_gen.close()

    add_evidence(
        opp["id"],
        claim="Some claim",
        stance="CONTRADICTS",
        found_at=datetime(2026, 1, 15),
    )

    body = client.get(f"/api/opportunities/{opp['id']}/dossier").json()
    assert body["research_summary"] == "Synthesis of the evidence so far."
    # stance is relative to this row's own claim, never to opportunity.thesis
    assert body["claims"][0]["contradicts"][0]["found_at"].startswith("2026-01-15")
