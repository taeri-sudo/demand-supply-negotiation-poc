from sop.forecast_candidate_selection import select_forecast_candidate
from sop.state import ForecastCandidate


def test_select_forecast_candidate_picks_highest_confidence():
    candidates = [
        ForecastCandidate(scenario="a", value=100.0, confidence=0.9, cost_estimate=500.0),
        ForecastCandidate(scenario="b", value=120.0, confidence=0.6, cost_estimate=450.0),
        ForecastCandidate(scenario="c", value=90.0, confidence=0.75, cost_estimate=520.0),
    ]

    selection = select_forecast_candidate(candidates)

    assert selection.judgment == {"selected": "a", "value": 100.0}
    assert "a" in selection.reasoning
