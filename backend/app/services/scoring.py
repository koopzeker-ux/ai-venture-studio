WEIGHTS = {
    "demand_evidence": 15,
    "problem_severity": 10,
    "purchase_intent": 10,
    "market_growth": 5,
    "competition_gap": 10,
    "distribution_potential": 10,
    "unit_economics": 10,
    "recurring_potential": 5,
    "speed_to_validation": 5,
    "automation_scalability": 5,
    "defensibility": 5,
    "capital_efficiency": 5,
    "risk": 5,
}


def calculate_score(values: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Each input factor is 0..10. Risk is 10 when LOW risk and 0 when HIGH risk."""
    weighted: dict[str, float] = {}
    total = 0.0
    for factor, weight in WEIGHTS.items():
        value = max(0.0, min(10.0, float(values.get(factor, 0))))
        points = (value / 10.0) * weight
        weighted[factor] = round(points, 2)
        total += points
    return round(total, 2), weighted


def should_alert(score: float, evidence_confidence: float) -> bool:
    return score >= 80 and evidence_confidence >= 75
