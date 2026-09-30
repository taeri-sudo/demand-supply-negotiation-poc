import pytest

from sop.forecast_scenario_selection import select_forecast_scenario
from sop.state import Scenario


def test_select_forecast_scenario_picks_lowest_cost_estimate():
    scenarios = [
        Scenario(scenario_id="a", value=100.0, likelihood=0.5, cost_estimate=500.0),
        Scenario(scenario_id="b", value=120.0, likelihood=0.3, cost_estimate=450.0),
        Scenario(scenario_id="c", value=90.0, likelihood=0.2, cost_estimate=520.0),
    ]

    selection = select_forecast_scenario(scenarios)

    assert selection.judgment == {"selected": "b", "value": 120.0}
    assert "b" in selection.reasoning


def test_select_forecast_scenario_rejects_scenarios_not_yet_computed():
    """value/cost_estimate 계산 전 시나리오는 선택 대상이 아니다."""
    with pytest.raises(ValueError):
        select_forecast_scenario([Scenario(scenario_id="a")])
