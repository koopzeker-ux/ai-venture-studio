import app.api.routes as routes_module

ALL_MAX_FACTORS = {
    k: 10
    for k in [
        "demand_evidence", "problem_severity", "purchase_intent", "market_growth",
        "competition_gap", "distribution_potential", "unit_economics",
        "recurring_potential", "speed_to_validation", "automation_scalability",
        "defensibility", "capital_efficiency", "risk",
    ]
}
ALL_LOW_FACTORS = {k: 1 for k in ALL_MAX_FACTORS}


def _create_opportunity(client, slug="test-opp", title="Test Opportunity"):
    resp = client.post(
        "/api/opportunities",
        json={"slug": slug, "title": title, "thesis": "test thesis"},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


def test_low_score_does_not_trigger_alert(client, monkeypatch):
    calls = []

    async def fake_send(text):
        calls.append(text)
        return True

    monkeypatch.setattr(routes_module, "send_telegram_message", fake_send)

    opp_id = _create_opportunity(client, slug="low-score-opp")
    resp = client.post(
        f"/api/opportunities/{opp_id}/score",
        json={"factors": ALL_LOW_FACTORS, "evidence_confidence": 50},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["telegram_alert_sent"] is False
    assert calls == []


def test_high_score_with_high_confidence_triggers_alert(client, monkeypatch):
    calls = []

    async def fake_send(text):
        calls.append(text)
        return True

    monkeypatch.setattr(routes_module, "send_telegram_message", fake_send)

    opp_id = _create_opportunity(client, slug="high-score-opp", title="High Score Opportunity")
    resp = client.post(
        f"/api/opportunities/{opp_id}/score",
        json={"factors": ALL_MAX_FACTORS, "evidence_confidence": 90},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 100
    assert body["telegram_alert_sent"] is True
    assert len(calls) == 1
    assert "High Score Opportunity" in calls[0]


def test_high_score_with_low_confidence_does_not_trigger_alert(client, monkeypatch):
    calls = []

    async def fake_send(text):
        calls.append(text)
        return True

    monkeypatch.setattr(routes_module, "send_telegram_message", fake_send)

    opp_id = _create_opportunity(client, slug="unconfirmed-opp")
    resp = client.post(
        f"/api/opportunities/{opp_id}/score",
        json={"factors": ALL_MAX_FACTORS, "evidence_confidence": 50},
    )

    assert resp.status_code == 200
    assert resp.json()["telegram_alert_sent"] is False
    assert calls == []


def test_telegram_outage_does_not_break_scoring_response(client, monkeypatch):
    """If Telegram is unavailable, the score must still be persisted and
    the API must still return 200 — an alert-channel outage is not a
    scoring failure."""

    async def fake_send_failure(text):
        return False

    monkeypatch.setattr(routes_module, "send_telegram_message", fake_send_failure)

    opp_id = _create_opportunity(client, slug="telegram-down-opp")
    resp = client.post(
        f"/api/opportunities/{opp_id}/score",
        json={"factors": ALL_MAX_FACTORS, "evidence_confidence": 90},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["score"] == 100
    assert body["telegram_alert_sent"] is False

    listed = client.get("/api/opportunities").json()
    scored = next(x for x in listed if x["id"] == opp_id)
    assert scored["score"] == 100
    assert scored["status"] == "scored"
