"""forecast agent 내부 단계(시나리오 정의·데이터 수집·예측 계산) 스텁 (M1).

실물 구현은 M2가 맡는다(MILESTONES.md M2). 그 전까지는 item_id별로 하드코딩된
시나리오 3개를 반환해, 시나리오 선택 판단(`forecast_scenario_selection.py`)과 그
이후 배분(`forecast_supply_allocation.py`)을 인스턴스별로 독립적으로 검증할 수
있게 한다. M2 5단계에서 실물 로직으로 교체되며 이 파일은 삭제된다.
"""

from .state import Scenario

_STUB_SCENARIOS_BY_ITEM: dict[str, list[Scenario]] = {
    "RAMEN": [
        Scenario(scenario_id="a", value=100.0, likelihood=0.5, cost_estimate=500.0),
        Scenario(scenario_id="b", value=120.0, likelihood=0.3, cost_estimate=450.0),
        Scenario(scenario_id="c", value=90.0, likelihood=0.2, cost_estimate=520.0),
    ],
    "SNACK": [
        Scenario(scenario_id="a", value=60.0, likelihood=0.5, cost_estimate=300.0),
        Scenario(scenario_id="b", value=80.0, likelihood=0.3, cost_estimate=280.0),
        Scenario(scenario_id="c", value=70.0, likelihood=0.2, cost_estimate=310.0),
    ],
}


def get_stub_scenarios(company_id: str, item_id: str) -> list[Scenario]:
    """item_id별로 다른 시나리오 3개를 반환한다.

    company_id는 인스턴스 식별자의 절반(STATE_SCHEMA.md "forecast_records[]")
    이지만, 실물 데이터 소스 판단이 아직 없어 이 스텁에서는 결과에 반영하지
    않는다 — 실물 구현이 붙을 때 회사별 분기가 필요해져도 호출부 시그니처는 이미
    (company_id, item_id)를 받고 있어 바뀌지 않는다.
    """
    return _STUB_SCENARIOS_BY_ITEM.get(item_id, _STUB_SCENARIOS_BY_ITEM["RAMEN"])
