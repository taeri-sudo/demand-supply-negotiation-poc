"""통계 라이브러리 호출을 모아 둔 어댑터.

statsforecast(예측기법)와 statsmodels(계절 분해)를 import하는 곳은 이 모듈뿐이다. agent
판단 코드는 이 모듈의 함수만 부르므로 라이브러리를 바꿔도 판단 로직은 바뀌지 않는다.
자체 알고리즘 개발과 하이퍼파라미터 튜닝은 하지 않고 표준 라이브러리 함수를 그대로 호출한다
(AGENT_NODE_LIST.md forecast agent 3단계).
"""

from dataclasses import dataclass

import numpy as np

from .judgment_thresholds import WINDOW_AVERAGE_MONTHS

# 우리가 쓰는 예측기법 이름 -> statsforecast 모델
METHODS = (
    "ets",  # 지수평활(AutoETS) — 계절 주기가 주어지면 계절 성분 포함
    "seasonal_naive",  # 1주기 전 같은 시점 값
    "window_average",  # 최근 몇 개월 평균
    "historic_average",  # 전체 평균
    "naive",  # 직전 값
    "croston_sba",  # 간헐수요용 Croston(SBA 보정)
)


@dataclass
class Decomposition:
    trend: np.ndarray
    seasonal: np.ndarray
    resid: np.ndarray


def _model(method: str, season_length: int):
    from statsforecast.models import (
        AutoETS,
        CrostonSBA,
        HistoricAverage,
        Naive,
        SeasonalNaive,
        WindowAverage,
    )

    if method == "ets":
        return AutoETS(season_length=season_length)
    if method == "seasonal_naive":
        return SeasonalNaive(season_length=season_length)
    if method == "window_average":
        return WindowAverage(window_size=WINDOW_AVERAGE_MONTHS)
    if method == "historic_average":
        return HistoricAverage()
    if method == "naive":
        return Naive()
    if method == "croston_sba":
        return CrostonSBA()
    raise ValueError(f"알 수 없는 예측기법: {method}")


def forecast_next(method: str, y: np.ndarray, season_length: int = 1, horizon: int = 1) -> np.ndarray:
    """`y`(월별 시계열)의 다음 `horizon`개월 평균 예측."""
    values = np.asarray(y, dtype=float)
    result = _model(method, season_length).forecast(y=values, h=horizon)
    return np.asarray(result["mean"], dtype=float)


def rolling_one_step_mae(
    method: str, y: np.ndarray, season_length: int, n_eval: int, min_train: int
) -> float:
    """마지막 `n_eval`개월을 한 달 앞 예측으로 평가한 평균 절대 오차(MAE).

    기준 시점을 한 달씩 밀며(rolling origin) 그 시점까지의 데이터로 다음 달을 예측해 실제값과
    비교한다. 계절 기법은 학습 길이가 계절 주기보다 짧으면 평가할 수 없어 NaN을 반환한다.
    """
    values = np.asarray(y, dtype=float)
    errors = []
    for origin in range(len(values) - n_eval, len(values)):
        if origin < min_train:
            return float("nan")
        if method in ("seasonal_naive",) and origin < season_length:
            return float("nan")
        prediction = forecast_next(method, values[:origin], season_length)[0]
        errors.append(abs(values[origin] - prediction))
    return float(np.mean(errors))


def decompose(values: np.ndarray, period: int) -> Decomposition | None:
    """강건 STL로 추세·계절·잔차로 나눈다. 시리즈가 2주기보다 짧거나 분해가 실패하면 None."""
    from statsmodels.tsa.seasonal import STL

    values = np.asarray(values, dtype=float)
    if len(values) < 2 * period + 1:
        return None
    try:
        fit = STL(values, period=period, robust=True).fit()
    except Exception:  # 분해 실패는 호출부가 대체 방법(중앙값 창)으로 처리한다
        return None
    return Decomposition(
        trend=np.asarray(fit.trend), seasonal=np.asarray(fit.seasonal), resid=np.asarray(fit.resid)
    )


def seasonal_strength(values: np.ndarray, period: int) -> float | None:
    """STL 기반 계절성 강도 Fs = max(0, 1 - Var(잔차) / Var(계절 + 잔차)). 분해 불가면 None."""
    parts = decompose(values, period)
    if parts is None:
        return None
    denominator = np.var(parts.seasonal + parts.resid)
    if denominator <= 0:
        return 0.0
    return float(max(0.0, 1.0 - np.var(parts.resid) / denominator))
