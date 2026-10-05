"""forecast agent 내부 단계(가정 정의·데이터 수집·요청량 계산) 스텁 (M1).

실물 구현은 M2가 맡는다(MILESTONES.md M2). 그 전까지는 item_id별로 하드코딩된
가정 3개를 반환해, 가정 선택 판단(`forecast_assumption_selection.py`)과 그
이후 배분(`forecast_supply_allocation.py`)을 인스턴스별로 독립적으로 검증할 수
있게 한다. M2 5단계에서 실물 로직으로 교체되며 이 파일은 삭제된다.
"""

from .state import Assumption

_STUB_ASSUMPTIONS_BY_ITEM: dict[str, list[Assumption]] = {
    "RAMEN": [
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=120.0, forecast_uncertainty=6.0),
        Assumption(assumption_id="c", value=90.0, forecast_uncertainty=15.0),
    ],
    "SNACK": [
        Assumption(assumption_id="a", value=60.0, forecast_uncertainty=9.0),
        Assumption(assumption_id="b", value=80.0, forecast_uncertainty=4.0),
        Assumption(assumption_id="c", value=70.0, forecast_uncertainty=10.0),
    ],
}


def get_stub_assumptions(company_id: str, item_id: str) -> list[Assumption]:
    """item_id별로 다른 가정 3개를 반환한다.

    company_id는 인스턴스 식별자의 절반(STATE_SCHEMA.md "forecast_records[]")
    이지만, 실물 데이터 소스 판단이 아직 없어 이 스텁에서는 결과에 반영하지
    않는다 — 실물 구현이 붙을 때 회사별 분기가 필요해져도 호출부 시그니처는 이미
    (company_id, item_id)를 받고 있어 바뀌지 않는다.
    """
    return _STUB_ASSUMPTIONS_BY_ITEM.get(item_id, _STUB_ASSUMPTIONS_BY_ITEM["RAMEN"])
