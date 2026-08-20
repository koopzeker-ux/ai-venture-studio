from app.services.scoring import calculate_score, should_alert


def test_score_maxes_at_100():
    values = {k: 10 for k in [
        "demand_evidence", "problem_severity", "purchase_intent", "market_growth",
        "competition_gap", "distribution_potential", "unit_economics",
        "recurring_potential", "speed_to_validation", "automation_scalability",
        "defensibility", "capital_efficiency", "risk"
    ]}
    score, _ = calculate_score(values)
    assert score == 100


def test_alert_threshold():
    assert should_alert(80, 75)
    assert not should_alert(79.9, 90)
    assert not should_alert(90, 74.9)
