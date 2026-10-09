"""`forecast_validation`: forecast 기록의 가정 검증 조건을 확인하는 검증agent의 규칙 (공통 틀은 `validation_agent.py`).

규칙은 `forecast_assumption_validation.py`의 가정 검증 조건 4개이고 `assumption` 유형의 의심되는 원인만 보낸다.
forecast의 계산을 재현하지 않는다. 가정이 선언한 근거가 기록된 `data_sources`에 있는지, 가정의 값이 과거 월별 요청량
범위 안인지처럼 forecast가 스스로 확인할 수 없는 것만 본다. 과거 월별 요청량은 forecast 기록에 없으므로 외부 데이터
인터페이스에서 읽는 함수(`HistoryProvider`)를 주입받는다. 검증agent는 State와 이번 주기 스냅샷만 읽으므로
실제 제공 함수(`snapshot_history_provider`)는 forecast와 같은 기준일(`data_end`)로 잘라 읽는다.
"""

from collections.abc import Callable
from pathlib import Path

import pandas as pd

from .data_source_judgment import last_complete_month
from .external_data import load_data_end, load_orders
from .forecast_assumption_validation import validate_assumptions
from .record_state import FORECAST_VALIDATOR_ROLE_TAG
from .state import ForecastRecord, SuspectedCause
from .validation_agent import ValidationRules

VALIDATOR_ROLE_TAG = FORECAST_VALIDATOR_ROLE_TAG
WRITER_ROLE_TAG = "forecast"

# (company_id, item_id) → 그 인스턴스의 과거 월별 요청량
HistoryProvider = Callable[[str, str], list[float]]


def monthly_order_history(orders: pd.DataFrame, data_end: pd.Timestamp) -> list[float]:
    """주문 이력의 월별 합계(완전한 달만, 주문 없는 달은 0). 정제나 `use_from`을 적용하지 않은 원본 기준이다."""
    if orders.empty:
        return []
    last = last_complete_month(data_end)
    months = orders["order_date"].dt.to_period("M").dt.to_timestamp()
    monthly = orders["quantity"].groupby(months).sum()
    monthly = monthly[monthly.index <= last]
    if monthly.empty:
        return []
    full = monthly.reindex(pd.date_range(monthly.index.min(), last, freq="MS"), fill_value=0.0)
    return [float(v) for v in full]


def snapshot_history_provider(
    data_dir: str | Path | None = None, data_end: pd.Timestamp | None = None
) -> HistoryProvider:
    """이번 주기 스냅샷의 주문 이력에서 인스턴스의 과거 월별 요청량을 읽는 함수.

    기준일은 forecast가 받은 입력과 같은 스냅샷 기준일(`load_data_end`)이다. 테스트에서 기준일을 옮길 때는 `data_end`를 준다.
    """
    snapshot_end = pd.Timestamp(data_end) if data_end is not None else load_data_end(data_dir)
    orders = load_orders(data_dir)

    def history_of(company_id: str, item_id: str) -> list[float]:
        own = orders[(orders["company_id"] == company_id) & (orders["item_id"] == item_id)]
        return monthly_order_history(own, snapshot_end)

    return history_of


def forecast_validation_rules(history_of: HistoryProvider) -> ValidationRules:
    """forecast 기록 하나의 검증 규칙. 인스턴스는 `agent_id`다."""

    def check(record: ForecastRecord) -> list[SuspectedCause]:
        history = history_of(record.company_id, record.item_id)
        return validate_assumptions(record.assumptions, record.data_sources, history)

    return ValidationRules(instance_id=lambda record: record.agent_id, check=check)
