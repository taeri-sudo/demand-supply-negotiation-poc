"""category_trend 원인 확인 — 시장 변화율이 우리 수요 변화율에 주는 영향이 통계적으로 있는지 판단한다.

AGENT_NODE_LIST.md forecast agent "통계기법 선택"의 첫 판단(원인 확인)에서 쓴다
(`forecast_driver_check.py`). 효과의 크기는 여기서 정하지 않는다 — 시장 변화율은 회귀 기법의
설명변수로 들어가고(`driver_regressors.py`), 요청량은 기법이 계산한다.

- **변화율**: 시장과 우리 수요 모두 계절 성분(STL)을 뺀 값의 로그 수준을 전월 차분한 것. 변환은
  이것 하나로 고정한다. 우리 수요는 같은 item의 POS(월별)다 — 주문은 수주 생성기가 만든 값이라
  시장과의 관계 근거로 부적합하다(실무에서는 실제 주문 이력, DESIGN.md "실무 전환 시 고려사항").
- **시차**: `TREND_LAG_MONTHS` 하나로 고정. 계획 시점(달 t가 끝난 뒤)에 알 수 있는 가장 최근 시장 값은
  공표 시차 때문에 t-2이고 예측 대상은 t+1이므로 3개월이다. 추정 결과를 보고 고르지 않는다.
- **추정**: 우리 수요 변화율을 시장 변화율로 설명하는 단순 회귀. 표준오차는 Newey-West(HAC)이고
  신뢰구간은 95%다.
- **판단**: 신뢰구간이 0을 포함하면 유의한 효과 없음(`no_significant_effect`)으로 이 원인을 단 가정을 만들지 않고, 포함하지 않으면 원인을 유지한다(`significant_effect`).
  0과 가까스로 걸치면(`TREND_ZERO_EDGE_FRACTION`) `ambiguous`로 표시한다.

반환은 다른 판단과 같은 `StructuredJudgment`({판단값, 근거})이며 LLM 판단(M7)이 같은 스키마로
이 자리를 이어받는다.
"""

import numpy as np
import pandas as pd

from . import stats_adapter
from .judgment import StructuredJudgment
from .judgment_thresholds import (
    MARKET_PUBLICATION_LAG_MONTHS,
    MONTHLY_SEASON_PERIOD,
    TREND_CONFIDENCE_LEVEL,
    TREND_LAG_MONTHS,
    TREND_MIN_OBSERVATIONS,
    TREND_ZERO_EDGE_FRACTION,
)
from .logging_utils import log

ROLE_TAG = "forecast"


def deseasonalized_log_change(level: pd.Series) -> pd.Series | None:
    """계절 성분(STL)을 뺀 값의 로그 수준을 전월 차분한 변화율.

    앞뒤 결측은 자르고 중간 결측이 있거나, 계절 분해를 못 하거나, 계절 성분을 뺀 값이 0 이하이면
    로그를 취할 수 없어 None을 반환한다.
    """
    trimmed = level.loc[level.first_valid_index() : level.last_valid_index()] if level.notna().any() else level
    if trimmed.empty or trimmed.isna().any():
        return None
    parts = stats_adapter.decompose(trimmed.to_numpy(dtype=float), MONTHLY_SEASON_PERIOD)
    if parts is None:
        return None
    adjusted = trimmed.to_numpy(dtype=float) - parts.seasonal
    if (adjusted <= 0).any():
        return None
    return pd.Series(np.diff(np.log(adjusted)), index=trimmed.index[1:])


def market_change_rate(group_index: pd.DataFrame | pd.Series) -> pd.Series | None:
    """시장 변화율. 그룹이 여럿이면 그룹별 변화율을 단순 평균한다("식품 가공 전체 흐름").

    그룹이 하나(D코드 하나로 특정되는 상품군)면 가중이나 평균 없이 그 변화율을 그대로 쓴다.
    그룹 하나라도 변화율을 만들 수 없으면 None이다.
    """
    frame = group_index.to_frame() if isinstance(group_index, pd.Series) else group_index
    rates = []
    for column in frame.columns:
        rate = deseasonalized_log_change(frame[column])
        if rate is None:
            return None
        rates.append(rate)
    return pd.concat(rates, axis=1, join="inner").mean(axis=1)


def _no_evidence(reason: str, **values: object) -> StructuredJudgment:
    """데이터가 모자라 추정 자체를 못 한 경우."""
    return StructuredJudgment(
        judgment={"decision": "no_evidence", "lag_months": TREND_LAG_MONTHS, **values},
        reasoning=f"category_trend 원인 근거 없음: {reason}",
    )


def estimate_category_trend(
    demand_monthly: pd.Series, market_index: pd.DataFrame | pd.Series, planning_month: pd.Timestamp
) -> StructuredJudgment:
    """우리 수요(월별 POS)와 시장 지수(물가 보정 후, 그룹별)로 category_trend 원인을 판단한다.

    `planning_month`는 마지막으로 끝난 달 t이다(예측 대상은 t+1). 시장은 공표된 달(t-2)까지만 쓴다.
    """
    cutoff = planning_month - pd.DateOffset(months=MARKET_PUBLICATION_LAG_MONTHS)
    market = market_index[market_index.index <= cutoff]
    market_rate = market_change_rate(market)
    demand_rate = deseasonalized_log_change(demand_monthly[demand_monthly.index <= planning_month])
    if market_rate is None or demand_rate is None:
        return _no_evidence("시장 또는 우리 수요의 변화율을 만들 수 없음(이력 부족, 결측, 계절 성분을 뺀 값이 0 이하)")

    latest_month = market_rate.index[-1]
    if latest_month != cutoff:
        return _no_evidence(
            f"가장 최근 시장 변화율이 {latest_month:%Y-%m}로 계획 시점에 알 수 있는 {cutoff:%Y-%m}이 아님"
        )

    shifted = pd.Series(
        market_rate.to_numpy(), index=market_rate.index + pd.DateOffset(months=TREND_LAG_MONTHS)
    )
    paired = pd.concat([demand_rate.rename("y"), shifted.rename("x")], axis=1, join="inner").dropna()
    if len(paired) < TREND_MIN_OBSERVATIONS:
        return _no_evidence(
            f"겹치는 변화율 관측이 {len(paired)}개로 기준({TREND_MIN_OBSERVATIONS}개) 미만", n=len(paired)
        )

    fit = stats_adapter.ols_slope_hac(
        paired["y"].to_numpy(), paired["x"].to_numpy(), TREND_CONFIDENCE_LEVEL
    )
    half_width = (fit.ci_high - fit.ci_low) / 2
    includes_zero = fit.ci_low <= 0 <= fit.ci_high
    nearest = min(abs(fit.ci_low), abs(fit.ci_high)) if not includes_zero else min(-fit.ci_low, fit.ci_high)
    near_zero = half_width > 0 and nearest / half_width <= TREND_ZERO_EDGE_FRACTION

    basis = {
        "coefficient": round(fit.coefficient, 4),
        "ci_low": round(fit.ci_low, 4),
        "ci_high": round(fit.ci_high, 4),
        "confidence": TREND_CONFIDENCE_LEVEL,
        "lag_months": TREND_LAG_MONTHS,
        "n": fit.n,
        "hac_maxlags": fit.maxlags,
    }
    summary = (
        f"계수 {fit.coefficient:.3f}, {TREND_CONFIDENCE_LEVEL:.0%} 신뢰구간 [{fit.ci_low:.3f}, {fit.ci_high:.3f}], "
        f"시차 {TREND_LAG_MONTHS}개월, n={fit.n}"
    )
    edge_reason = "신뢰구간이 0과 가까스로 걸쳐 있어(반폭의 " f"{nearest / half_width:.0%}) 근거 판단이 불안정" if near_zero else None

    if includes_zero:
        log(ROLE_TAG, "estimate_category_trend", decision="no_significant_effect", n=fit.n, ambiguous=near_zero)
        return StructuredJudgment(
            judgment={"decision": "no_significant_effect", **basis},
            reasoning=f"category_trend 원인 없음: {summary} — 신뢰구간이 0을 포함해 유의한 효과가 없음",
            ambiguous=near_zero,
            ambiguity_reason=edge_reason,
        )

    log(ROLE_TAG, "estimate_category_trend", decision="significant_effect", n=fit.n, ambiguous=near_zero)
    return StructuredJudgment(
        judgment={"decision": "significant_effect", **basis},
        reasoning=f"{summary} — 신뢰구간이 0을 포함하지 않아 시장 변화율을 설명변수로 유지",
        ambiguous=near_zero,
        ambiguity_reason=edge_reason,
    )
