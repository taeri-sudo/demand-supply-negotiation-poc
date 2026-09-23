"""analysis agent 스텁 (M1).

실물 analysis agent(데이터 소스 판단·모델 선택·시나리오 계산)의 실제 구현은
아직 배정된 마일스톤이 없다(DESIGN.md "아직 결정 안 된 것" 참고 — 원래
M3가 맡았으나 analysis/forecast 통합으로 M3 자체가 성립하지 않게 되며
생긴 공백). 그 전까지는 item_id별로 하드코딩된 a/b/c candidate를 반환해,
forecast agent의 후보 선택 판단(`forecast_candidate_selection.py`)과 그
이후 배분(`forecast_supply_allocation.py`)을 인스턴스별로 독립적으로
검증할 수 있게 한다.
"""

from .state import ForecastCandidate

_STUB_CANDIDATES_BY_ITEM: dict[str, list[ForecastCandidate]] = {
    "RAMEN": [
        ForecastCandidate(scenario="a", value=100.0, confidence=0.9, cost_estimate=500.0),
        ForecastCandidate(scenario="b", value=120.0, confidence=0.6, cost_estimate=450.0),
        ForecastCandidate(scenario="c", value=90.0, confidence=0.75, cost_estimate=520.0),
    ],
    "SNACK": [
        ForecastCandidate(scenario="a", value=60.0, confidence=0.5, cost_estimate=300.0),
        ForecastCandidate(scenario="b", value=80.0, confidence=0.85, cost_estimate=280.0),
        ForecastCandidate(scenario="c", value=70.0, confidence=0.7, cost_estimate=310.0),
    ],
}


def get_stub_candidates(company_id: str | None, item_id: str) -> list[ForecastCandidate]:
    """item_id별로 다른 candidate 3개를 반환한다.

    company_id는 인스턴스 식별자의 절반(STATE_SCHEMA.md "forecast_agents[]")
    이지만, 실물 data_source_basis 판단(자사/유사업종 데이터 선택)이 아직
    없어(위 모듈 docstring 참고) 이 스텁에서는 결과에 반영하지 않는다 —
    실물 구현이 붙을 때 회사별 분기가 필요해져도 호출부 시그니처는 이미
    (company_id, item_id)를 받고 있어 바뀌지 않는다.
    """
    return _STUB_CANDIDATES_BY_ITEM.get(item_id, _STUB_CANDIDATES_BY_ITEM["RAMEN"])
