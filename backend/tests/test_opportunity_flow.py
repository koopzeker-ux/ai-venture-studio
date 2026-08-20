from app.api import routes as routes_module

FULL_FACTORS = {
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


def create_opportunity(client, slug="test-opp"):
    return client.post(
        "/api/opportunities",
        json={"slug": slug, "title": "Test Opp", "thesis": "thesis text"},
    )


async def _no_alert(message):
    return False


def test_opportunity_moves_through_pipeline(client, monkeypatch):
    monkeypatch.setattr(routes_module, "send_telegram_message", _no_alert)

    created = create_opportunity(client)
    assert created.status_code == 200
    assert created.json()["status"] == "discovered"
    opportunity_id = created.json()["id"]

    listed = client.get("/api/opportunities").json()
    assert listed[0]["status"] == "discovered"
    assert listed[0]["score"] is None
    assert listed[0]["evidence_confidence"] is None

    scored = client.post(
        f"/api/opportunities/{opportunity_id}/score",
        json={"factors": FULL_FACTORS, "evidence_confidence": 80},
    )
    assert scored.status_code == 200
    body = scored.json()
    assert body["score"] == 100
    assert body["evidence_confidence"] == 80

    listed_after = client.get("/api/opportunities").json()
    assert listed_after[0]["status"] == "scored"
    assert listed_after[0]["score"] == 100
    assert listed_after[0]["evidence_confidence"] == 80


def test_duplicate_slug_rejected(client):
    create_opportunity(client, slug="dup-slug")
    dup = create_opportunity(client, slug="dup-slug")
    assert dup.status_code == 409


def test_score_unknown_opportunity_returns_404(client):
    response = client.post(
        "/api/opportunities/9999/score",
        json={"factors": FULL_FACTORS, "evidence_confidence": 80},
    )
    assert response.status_code == 404


def test_alert_fires_only_when_score_and_confidence_both_pass(client, monkeypatch):
    sent_messages = []

    async def _record_alert(message):
        sent_messages.append(message)
        return True

    monkeypatch.setattr(routes_module, "send_telegram_message", _record_alert)

    high_score_low_confidence = create_opportunity(client, slug="high-score-low-confidence")
    oid = high_score_low_confidence.json()["id"]
    result = client.post(
        f"/api/opportunities/{oid}/score",
        json={"factors": FULL_FACTORS, "evidence_confidence": 50},
    ).json()
    assert result["score"] == 100
    assert result["telegram_alert_sent"] is False

    low_score_high_confidence = create_opportunity(client, slug="low-score-high-confidence")
    oid2 = low_score_high_confidence.json()["id"]
    result2 = client.post(
        f"/api/opportunities/{oid2}/score",
        json={"factors": {}, "evidence_confidence": 100},
    ).json()
    assert result2["score"] == 0
    assert result2["telegram_alert_sent"] is False

    assert sent_messages == []

    both_pass = create_opportunity(client, slug="both-pass")
    oid3 = both_pass.json()["id"]
    result3 = client.post(
        f"/api/opportunities/{oid3}/score",
        json={"factors": FULL_FACTORS, "evidence_confidence": 80},
    ).json()
    assert result3["telegram_alert_sent"] is True
    assert len(sent_messages) == 1


def test_evidence_confidence_out_of_range_rejected(client):
    created = create_opportunity(client, slug="range-check")
    oid = created.json()["id"]
    response = client.post(
        f"/api/opportunities/{oid}/score",
        json={"factors": FULL_FACTORS, "evidence_confidence": 150},
    )
    assert response.status_code == 422
