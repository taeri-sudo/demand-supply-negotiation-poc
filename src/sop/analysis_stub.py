"""analysis agent 스텁 (M1).

실물 analysis agent(데이터 소스 판단·모델 선택·시나리오 계산)는 M3에서
구현한다. 그 전까지는 하드코딩된 a/b/c candidate를 반환해, forecast agent의
후보 선택 판단(`forecast_candidate_selection.py`)과 그 이후 협상
(`forecast_supply_allocation.py`)을 독립적으로 검증할 수 있게 한다.
"""

from .state import ForecastCandidate


def get_stub_candidates() -> list[ForecastCandidate]:
    return [
        ForecastCandidate(scenario="a", value=100.0, confidence=0.9, cost_estimate=500.0),
        ForecastCandidate(scenario="b", value=120.0, confidence=0.6, cost_estimate=450.0),
        ForecastCandidate(scenario="c", value=90.0, confidence=0.75, cost_estimate=520.0),
    ]
