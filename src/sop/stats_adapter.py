"""통계 라이브러리 호출을 모아 둔 어댑터.

statsforecast(예측기법)와 statsmodels(계절 분해, 회귀)를 import하는 곳은 이 모듈뿐이다. agent
판단 코드는 이 모듈의 함수만 부르므로 라이브러리를 바꿔도 판단 로직은 바뀌지 않는다.
자체 알고리즘 개발과 하이퍼파라미터 튜닝은 하지 않고 표준 라이브러리 함수를 그대로 호출한다
(AGENT_NODE_LIST.md forecast agent "통계기법 선택").
"""

import warnings
from dataclasses import dataclass

import numpy as np

from .judgment_thresholds import MIN_HISTORY_MONTHS, MONTHLY_SEASON_PERIOD, WINDOW_AVERAGE_MONTHS

# 우리가 쓰는 통계기법 이름. 앞의 여섯은 시계열 자체만 쓰고(driver를 반영할 수 없다), 뒤의 둘은
# driver의 데이터를 설명변수로 받는다(`REGRESSION_METHODS`) — 기법마다 반영하는 방식이 다르다.
METHODS = (
    "ets",  # 지수평활(AutoETS), 계절 성분 없음
    "ets_seasonal",  # 지수평활(AutoETS), 계절 주기 12 — 이력이 `MIN_HISTORY_MONTHS`개월 이상일 때만
    "seasonal_naive",  # 1주기 전 같은 시점 값
    "window_average",  # 최근 몇 개월 평균
    "historic_average",  # 전체 평균
    "naive",  # 직전 값
    "croston_sba",  # 간헐수요용 Croston(SBA 보정)
    "regression",  # 상수 + 계절 더미 + driver 설명변수의 최소제곱 회귀
    "regression_ar1",  # 같은 설명변수 + AR(1) 오차의 회귀(SARIMAX, 차수 고정)
)
REGRESSION_METHODS = ("regression", "regression_ar1")


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

    if method in ("ets", "ets_seasonal"):
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


def season_length_for(method: str, n_months: int) -> int:
    """기법이 쓰는 계절 주기. 계절 기법은 12, 회귀는 이력이 충분할 때만 12(계절 더미), 나머지는 1."""
    if method in ("seasonal_naive", "ets_seasonal"):
        return MONTHLY_SEASON_PERIOD
    if method in REGRESSION_METHODS:
        return MONTHLY_SEASON_PERIOD if n_months >= MIN_HISTORY_MONTHS else 1
    return 1


def _design(n: int, season_length: int, regressors: np.ndarray | None, x_future: np.ndarray | None) -> np.ndarray:
    """회귀 설계행렬((n + 1) × 열): 상수, 계절 더미(위치 mod 주기), driver 설명변수. 마지막 행이 예측 달."""
    columns = [np.ones(n + 1)]
    if season_length > 1:
        position = np.arange(n + 1) % season_length
        columns += [(position == k).astype(float) for k in range(1, season_length)]
    if regressors is not None and regressors.shape[1] > 0:
        if x_future is None:
            raise ValueError("설명변수에 예측 달의 값이 없음")
        stacked = np.vstack([regressors, np.asarray(x_future, dtype=float)[None, :]])
        columns += [stacked[:, j] for j in range(stacked.shape[1])]
    return np.column_stack(columns)


def _regression_forecast(
    method: str, y: np.ndarray, regressors: np.ndarray | None, x_future: np.ndarray | None
) -> float:
    import statsmodels.api as sm
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    n = len(y)
    design = _design(n, season_length_for(method, n), regressors, x_future)
    train, future = design[:n], design[n : n + 1]
    mask = np.isfinite(y) & np.isfinite(train).all(axis=1)
    if mask.sum() < design.shape[1] + 3:
        raise ValueError("회귀에 쓸 수 있는 관측이 모자람")
    if not np.isfinite(future).all():
        raise ValueError("예측 달의 설명변수 값이 없음")
    if method == "regression":
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # 결측 달 때문에 계절 더미가 비는 경우의 rank 경고(유사역행렬로 계산)
            fit = sm.OLS(y[mask], train[mask]).fit()
        return float(np.asarray(fit.predict(future))[0])
    exog = np.nan_to_num(train[:, 1:])  # 상수는 trend="c"가 맡는다. 결측 행은 endog를 비워 건너뛴다
    endog = np.where(mask, y, np.nan)
    model = SARIMAX(
        endog, exog=exog if exog.shape[1] else None, order=(1, 0, 0), trend="c"
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = model.fit(disp=False)
        forecast = fit.forecast(steps=1, exog=future[:, 1:] if exog.shape[1] else None)  # pyright: ignore[reportAttributeAccessIssue] -- statsmodels 스텁이 SARIMAX.fit()의 반환을 결과 객체가 아닌 배열 튜플로 추론하나 실제는 SARIMAXResults
    return float(np.asarray(forecast)[0])


def forecast_value(
    method: str, y: np.ndarray, regressors: np.ndarray | None = None, x_future: np.ndarray | None = None
) -> float:
    """`y`(월별 시계열, 결측 가능)의 다음 달 예측. 회귀 기법만 `regressors`(n × k)와 예측 달의 `x_future`(k)를 쓴다.

    driver를 반영할 수 없는 기법에 설명변수를 주거나, 이력이 모자라 계산할 수 없으면 ValueError를 낸다.
    """
    values = np.asarray(y, dtype=float)
    has_regressors = regressors is not None and regressors.shape[1] > 0
    if method in REGRESSION_METHODS:
        return _regression_forecast(method, values, regressors if has_regressors else None, x_future)
    if has_regressors:
        raise ValueError(f"{method}은 driver를 반영할 수 없음")
    n = len(values)
    if not np.isfinite(values).all():
        raise ValueError(f"{method}은 결측이 있는 시계열을 쓸 수 없음")
    if method == "seasonal_naive" and n < MONTHLY_SEASON_PERIOD:
        raise ValueError("계절 기법은 이력이 1주기 이상이어야 함")
    if method == "ets_seasonal" and n < MIN_HISTORY_MONTHS:
        raise ValueError("계절 지수평활은 이력이 2주기 이상이어야 함")
    return float(forecast_next(method, values, season_length_for(method, n))[0])


def walk_forward_mae(
    method: str, y: np.ndarray, origins: list[int], regressors: np.ndarray | None = None
) -> float:
    """기준 시점 `origins`(y의 위치)마다 그 시점까지의 데이터로 다음 달을 예측해 실제값과 비교한 평균 절대 오차.

    계산할 수 없는 시점이 하나라도 있으면 NaN이다(그 기법은 이 평가 구간에서 쓸 수 없다).
    """
    values = np.asarray(y, dtype=float)
    errors = []
    for origin in origins:
        try:
            prediction = forecast_value(
                method,
                values[:origin],
                regressors[:origin] if regressors is not None else None,
                regressors[origin] if regressors is not None else None,
            )
        except Exception:  # 평가 불가는 호출부가 기법 제외로 처리한다
            return float("nan")
        if not np.isfinite(prediction):
            return float("nan")
        errors.append(abs(values[origin] - prediction))
    return float(np.mean(errors)) if errors else float("nan")


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


@dataclass
class SlopeEstimate:
    """단순 회귀 y = a + b·x 의 기울기 추정 결과."""

    coefficient: float
    ci_low: float
    ci_high: float
    n: int
    maxlags: int


def newey_west_maxlags(n: int) -> int:
    """Newey-West(1994) 규칙 floor(4·(n/100)^(2/9)) — 자기상관에 강건한 표준오차의 시차 수."""
    return int(np.floor(4 * (n / 100) ** (2 / 9)))


def ols_slope_hac(y: np.ndarray, x: np.ndarray, confidence: float) -> SlopeEstimate:
    """`y`를 `x`로 설명하는 단순 회귀(상수항 포함)의 기울기와 신뢰구간.

    표준오차는 자기상관과 이분산에 강건한 Newey-West(HAC, Bartlett 커널)이고 표본이 작아
    소표본 보정과 t분포를 쓴다. 신뢰구간 `confidence`는 0.95처럼 수준으로 준다.
    """
    import statsmodels.api as sm

    y_arr = np.asarray(y, dtype=float)
    x_arr = np.asarray(x, dtype=float)
    maxlags = newey_west_maxlags(len(y_arr))
    fit = sm.OLS(y_arr, sm.add_constant(x_arr)).fit(
        cov_type="HAC", cov_kwds={"maxlags": maxlags, "use_correction": True}, use_t=True
    )
    low, high = np.asarray(fit.conf_int(alpha=1 - confidence))[1]
    return SlopeEstimate(
        coefficient=float(np.asarray(fit.params)[1]),
        ci_low=float(low),
        ci_high=float(high),
        n=len(y_arr),
        maxlags=maxlags,
    )
