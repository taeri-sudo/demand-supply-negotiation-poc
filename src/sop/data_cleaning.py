"""표준 정제(cleaning) — 오염(오류·이상치)을 걸러 대체값으로 바꾼다.

AGENT_NODE_LIST.md forecast agent "데이터 수집과 소스 판단"의 "오염 → 표준 정제 적용". 음수 값(반품·입력 오류)은
0으로 바꾸고, 계절성을 뺀 잔차가 중앙값 절대편차(MAD)의 k배를 넘게 벗어난 점은 이상치로
보고 추세+계절 적합값으로 바꾼다. 계절성을 먼저 빼므로 12월 성수기 같은 정상적인 계절 피크는
이상치로 잘리지 않는다. 판매량은 개수 자료라 적합값이 작을수록 변동의 최소 크기가 적합값의
제곱근(푸아송 변동)이므로, 잔차 척도가 이보다 작아지지 않게 하한을 둔다(0이 많은 간헐 시리즈에서
작은 값 1-2개가 이상치로 잘리는 것을 막는다). 기록 있는 프로모션 구간(`protected`)은 정상적인 수요 효과이므로
정제하지 않는다 — 프로모션 효과 계산이 그 구간에 의존한다.

시리즈가 2주기보다 짧아 계절 분해를 못 하면 중앙값 창(rolling median)을 적합값으로 쓴다.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import stats_adapter


@dataclass
class CleaningResult:
    cleaned: pd.Series
    flagged: pd.Series  # 정제된 점(음수 대체 + 이상치 대체)
    n_negative: int
    n_outliers: int

    @property
    def count(self) -> int:
        return int(self.flagged.sum())


def clean_series(
    values: pd.Series,
    period: int,
    k: float,
    fallback_window: int,
    protected: pd.Series | None = None,
) -> CleaningResult:
    x = values.astype(float).copy()
    keep = (
        protected.reindex(x.index).fillna(False).astype(bool)
        if protected is not None
        else pd.Series(False, index=x.index)
    )

    negative = x < 0
    x[negative] = 0.0

    parts = stats_adapter.decompose(x.to_numpy(), period)
    if parts is not None:
        fitted = pd.Series(parts.trend + parts.seasonal, index=x.index)
    else:
        fitted = x.rolling(fallback_window, center=True, min_periods=1).median()
    resid = x - fitted

    judged = resid[~keep]
    outlier = pd.Series(False, index=x.index)
    if len(judged) > 2:
        center = judged.median()
        scale = 1.4826 * (judged - center).abs().median()
        if scale == 0:
            scale = float(judged.std())
        local_scale = np.maximum(scale, np.sqrt(np.maximum(fitted.to_numpy(), 1.0)))
        outlier = ((resid - center).abs() > k * local_scale) & ~keep

    cleaned = x.where(~outlier, fitted.clip(lower=0.0))
    flagged = negative | outlier
    return CleaningResult(
        cleaned=cleaned,
        flagged=flagged,
        n_negative=int(negative.sum()),
        n_outliers=int(outlier.sum()),
    )
