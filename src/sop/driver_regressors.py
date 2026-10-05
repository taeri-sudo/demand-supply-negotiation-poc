"""driver의 데이터를 통계기법이 받는 설명변수로 바꾼다.

driver를 반영하는 방식은 기법마다 다르다. 이 모듈은 회귀 계열 기법(`stats_adapter.REGRESSION_METHODS`)이
설명변수로 받을 월별 열과 예측 달의 값을 만든다. 시계열만 쓰는 기법은 설명변수를 받지 못하므로
(`stats_adapter.forecast_value`가 거부) driver가 있는 가정에서는 제외된다.

- `category_trend`: 시장 변화율(계절 성분을 뺀 로그 수준의 전월 차분, `category_trend.py`와 같은 변환)을
  `TREND_LAG_MONTHS`개월 늦춘 값. 예측 달의 값은 계획 시점에 이미 공표된 가장 최근 시장 변화율이다.
- `event`(전제): 그 달에 기록된 프로모션이 있으면 1, 기록상 없으면 0, 기록이 없으면 결측. 예측 달은
  확정된 프로모션 일정이므로 1이다.

결측 열이 있는 달은 회귀의 학습과 평가에서 제외된다(`stats_adapter`).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .category_trend import market_change_rate
from .judgment_thresholds import MARKET_PUBLICATION_LAG_MONTHS, TREND_LAG_MONTHS
from .state import Driver


@dataclass
class Regressors:
    names: list[str]
    matrix: np.ndarray  # (훈련 월 수, 열 수) — 훈련 시리즈의 월과 같은 순서
    x_future: np.ndarray  # (열 수,) — 예측 달의 값


def _promotion_column(orders: pd.DataFrame, index: pd.DatetimeIndex) -> np.ndarray:
    month = orders["order_date"].dt.to_period("M").dt.to_timestamp()
    status: dict[pd.Timestamp, float] = {}
    for key, group in orders.groupby(month):
        promo = group["promotion"]
        if promo.eq(True).any():
            status[pd.Timestamp(str(key))] = 1.0
        elif promo.notna().all():
            status[pd.Timestamp(str(key))] = 0.0
    return np.array([status.get(m, np.nan) for m in index], dtype=float)


def _market_column(
    market: pd.DataFrame | pd.Series, index: pd.DatetimeIndex, planning_month: pd.Timestamp
) -> tuple[np.ndarray, float]:
    cutoff = planning_month - pd.DateOffset(months=MARKET_PUBLICATION_LAG_MONTHS)
    rate = market_change_rate(market[market.index <= cutoff])
    if rate is None:
        return np.full(len(index), np.nan), float("nan")
    column = np.array(
        [rate.get(m - pd.DateOffset(months=TREND_LAG_MONTHS), np.nan) for m in index], dtype=float
    )
    target = planning_month + pd.DateOffset(months=1)
    return column, float(rate.get(target - pd.DateOffset(months=TREND_LAG_MONTHS), np.nan))


def build_regressors(
    drivers: list[Driver],
    premises: list[Driver],
    orders: pd.DataFrame,
    market: pd.DataFrame | pd.Series | None,
    index: pd.DatetimeIndex,
    planning_month: pd.Timestamp,
) -> Regressors | None:
    """가정의 driver와 모든 가정의 전제로 설명변수를 만든다. 설명변수가 필요 없으면 None."""
    names: list[str] = []
    columns: list[np.ndarray] = []
    future: list[float] = []
    if any(p.driver == "event" for p in premises):
        names.append("event")
        columns.append(_promotion_column(orders, index))
        future.append(1.0)
    for driver in drivers:
        if driver.driver == "category_trend":
            if market is None:
                raise ValueError("category_trend는 시장 데이터가 있어야 함")
            column, value = _market_column(market, index, planning_month)
            names.append("category_trend")
            columns.append(column)
            future.append(value)
    if not names:
        return None
    return Regressors(names=names, matrix=np.column_stack(columns), x_future=np.array(future, dtype=float))
