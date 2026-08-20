"""Independent REVIEWER contract tests for M3.1's GET /api/opportunities/{id}.

Also re-confirms the pre-existing create/list/score routes still work
unchanged, since routes.py was touched to add the new endpoint.
"""
from sqlalchemy import select

from app.collectors.pipeline import process_raw_signals
from app.db.session import get_db
from app.main import app
from app.models.entities import Opportunity

FULL_SCORE_FACTORS = {
    "demand_evidence": 10,
    "problem_severity": 10,
    "purchase_intent": 10,
    "market_growth": 10,
    "competition_gap": 10,
    "distribution_potential": 10,
    "unit_economics": 10,
    "recurring_potential": 10,
    "speed_to_validation": 10,
    "automation_scalability": 10,
    "defensibility": 10,
    "capital_efficiency": 10,
    "risk": 10,
}


def _pipeline_signal_with_evidence(source_url="https://reviewer.example/detail-endpoint-source"):
    return {
        "source": "hackernews",
        "source_url": source_url,
        "title": "Detail endpoint fixture signal",
        "content": "wish there was a tool for validating this endpoint",
        "metadata": {"engagement_score": None, "published_at": None, "is_launch": False},
    }


def _create_opportunity_with_evidence_via_pipeline(client, source_url="https://reviewer.example/detail-endpoint-source"):
    """Runs process_raw_signals against the exact same in-memory DB the
    TestClient is wired to, so the created Opportunity+Evidence are
    visible through the API — same technique as the M2.1 M1-regression
    reviewer test."""
    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    try:
        result = process_raw_signals(db, [_pipeline_signal_with_evidence(source_url)])
        assert result["candidates_created"] == 1
        opportunity = db.scalars(select(Opportunity)).one()
        return opportunity.id
    finally:
        next(db_gen, None)


def test_detail_endpoint_returns_200_with_correct_opportunity_and_evidence(client):
    opportunity_id = _create_opportunity_with_evidence_via_pipeline(client)

    response = client.get(f"/api/opportunities/{opportunity_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == opportunity_id
    assert body["status"] == "discovered"
    assert body["score"] is None
    assert body["evidence_confidence"] is None
    assert body["title"] == "Detail endpoint fixture signal"

    assert len(body["evidence"]) == 1
    evidence = body["evidence"][0]
    assert evidence["evidence_type"] == "pain_point_signal"
    assert evidence["source"] == "hackernews"
    assert evidence["source_url"] == "https://reviewer.example/detail-endpoint-source"
    assert evidence["independently_confirmed"] is False


def test_detail_endpoint_unknown_id_returns_404(client):
    response = client.get("/api/opportunities/999999")
    assert response.status_code == 404


def test_detail_endpoint_does_not_mutate_the_database(client):
    opportunity_id = _create_opportunity_with_evidence_via_pipeline(
        client, source_url="https://reviewer.example/detail-endpoint-no-mutation"
    )

    first = client.get(f"/api/opportunities/{opportunity_id}").json()
    second = client.get(f"/api/opportunities/{opportunity_id}").json()

    assert first == second

    listed = client.get("/api/opportunities").json()
    entry = next(x for x in listed if x["id"] == opportunity_id)
    assert entry["status"] == "discovered"
    assert entry["score"] is None


def test_existing_create_list_score_routes_remain_intact(client, monkeypatch):
    """Lightweight end-to-end smoke check that the pre-existing M1 routes
    still function unchanged after routes.py gained the detail endpoint."""
    from app.api import routes as routes_module

    async def _no_alert(message):
        return False

    monkeypatch.setattr(routes_module, "send_telegram_message", _no_alert)

    created = client.post(
        "/api/opportunities",
        json={"slug": "m3-1-reviewer-smoke", "title": "M3.1 Reviewer Smoke", "thesis": "thesis text"},
    )
    assert created.status_code == 200
    opportunity_id = created.json()["id"]

    listed = client.get("/api/opportunities").json()
    assert any(x["id"] == opportunity_id for x in listed)

    scored = client.post(
        f"/api/opportunities/{opportunity_id}/score",
        json={"factors": FULL_SCORE_FACTORS, "evidence_confidence": 80},
    )
    assert scored.status_code == 200
    assert scored.json()["score"] == 100

    detail = client.get(f"/api/opportunities/{opportunity_id}").json()
    assert detail["status"] == "scored"
    assert detail["score"] == 100
    assert detail["evidence_confidence"] == 80
