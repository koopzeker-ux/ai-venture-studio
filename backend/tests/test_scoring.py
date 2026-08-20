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


def test_score_clamps_out_of_range_inputs():
    score, breakdown = calculate_score({"demand_evidence": -5, "problem_severity": 999})
    assert breakdown["demand_evidence"] == 0
    assert breakdown["problem_severity"] == 10
    assert score == 10


def test_score_missing_factors_default_to_zero():
    score, breakdown = calculate_score({})
    assert score == 0
    assert all(points == 0 for points in breakdown.values())


def test_score_and_evidence_confidence_are_independent_axes():
    high_score, _ = calculate_score({k: 10 for k in [
        "demand_evidence", "problem_severity", "purchase_intent", "market_growth",
        "competition_gap", "distribution_potential", "unit_economics",
        "recurring_potential", "speed_to_validation", "automation_scalability",
        "defensibility", "capital_efficiency", "risk"
    ]})
    # A perfect factor score must not by itself satisfy the alert gate;
    # evidence_confidence is a separate axis and must be evaluated on its own.
    assert high_score == 100
    assert not should_alert(high_score, evidence_confidence=0)
