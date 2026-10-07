"""가정 선택·배분 테스트가 쓰는 고정 가정 값. 가정 계산 결과를 대신하는 테스트 전용 입력이다."""

from sop.state import Assumption

_ASSUMPTIONS_BY_ITEM: dict[str, list[Assumption]] = {
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


def fixed_assumptions(item_id: str) -> list[Assumption]:
    """item_id별로 다른 가정 3개를 반환한다."""
    return _ASSUMPTIONS_BY_ITEM[item_id]
