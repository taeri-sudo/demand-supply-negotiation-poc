"""forecast agent 내부 3단계 — 예측기법 선택 (규칙 기반).

AGENT_NODE_LIST.md forecast agent 3단계: 데이터 특성에 따라 통계 예측기법을 고른다(LLM이 아니라
통계 기법). 순서는 두 단계다.

1. **특성으로 후보 좁히기**(우선순위 순): 간헐수요(평균 수요 간격 ADI가 `INTERMITTENT_ADI` 이상,
   Syntetos-Boylan) → Croston 계열, 이력이 `MIN_HISTORY_MONTHS`개월 미만 → 평균·이동평균·직전값,
   강한 계절성(STL 기반 강도가 `SEASONAL_STRENGTH_MIN` 이상) → 계절 지수평활·계절 naive, 그 외 →
   지수평활·이동평균·직전값.
2. **후보끼리 평가해 하나 고르기**: 계획 주기가 월이라 다음 달 예측이 용도이므로, 마지막
   `HOLDOUT_MONTHS`개월을 한 달 앞 예측으로 평가한다(기준 시점을 한 달씩 밀며 그 시점까지의 데이터로
   다음 달을 예측). 평가 구간은 실제 주문이 있는 달로만 잡는다 — 이력 부족으로 앞쪽을 보강한
   시리즈는 보강값이 학습에는 들어가도 평가에는 쓰지 않고, 실제 주문이 `MIN_HOLDOUT_MONTHS`개월
   미만이면 평가를 건너뛰고 "애매함"으로 표시한다. 오차 지표는 평균 절대 오차(MAE)이고 가장 낮은
   기법을 고른다.

상위 두 기법의 MAE 상대차가 `METHOD_AMBIGUITY_REL_DIFF` 이하이면 "애매함"으로 표시하고 규칙대로
(가장 낮은 기법) 선택한다. 되돌림으로 재실행될 때는 `exclude`에 직전 예측기법을 넘겨 제외한 채
재선택한다. 반환은 공통 `StructuredJudgment`다(공통 규칙 2).
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from . import stats_adapter
from .judgment import StructuredJudgment
from .judgment_thresholds import (
    HOLDOUT_MONTHS,
    INTERMITTENT_ADI,
    METHOD_AMBIGUITY_REL_DIFF,
    METHOD_ERROR_METRIC,
    MIN_HISTORY_MONTHS,
    MIN_HOLDOUT_MONTHS,
    MIN_TRAIN_MONTHS,
    MONTHLY_SEASON_PERIOD,
    SEASONAL_STRENGTH_MIN,
)


@dataclass
class SeriesCharacteristics:
    n_months: int
    zero_fraction: float
    adi: float | None  # 평균 수요 간격(전체 기간 / 수요가 있는 달 수)
    seasonal_strength: float | None
    kind: str  # intermittent | short | seasonal | regular


def describe_series(y: pd.Series) -> SeriesCharacteristics:
    values = y.to_numpy(dtype=float)
    n = len(values)
    nonzero = int((values > 0).sum())
    adi = n / nonzero if nonzero else None
    strength = (
        stats_adapter.seasonal_strength(values, MONTHLY_SEASON_PERIOD) if n >= MIN_HISTORY_MONTHS else None
    )
    if adi is None or adi >= INTERMITTENT_ADI:
        kind = "intermittent"
    elif n < MIN_HISTORY_MONTHS:
        kind = "short"
    elif strength is not None and strength >= SEASONAL_STRENGTH_MIN:
        kind = "seasonal"
    else:
        kind = "regular"
    return SeriesCharacteristics(
        n_months=n,
        zero_fraction=float((values == 0).mean()) if n else 1.0,
        adi=adi,
        seasonal_strength=strength,
        kind=kind,
    )


_CANDIDATES = {
    "intermittent": ["croston_sba", "window_average", "historic_average"],
    "short": ["window_average", "historic_average", "naive"],
    "seasonal": ["ets", "seasonal_naive", "window_average"],
    "regular": ["ets", "window_average", "naive"],
}


def candidate_methods(characteristics: SeriesCharacteristics) -> list[str]:
    return list(_CANDIDATES[characteristics.kind])


def select_forecast_method(
    y: pd.Series, exclude: set[str] | None = None, n_observed: int | None = None
) -> StructuredJudgment:
    """월별 시계열 `y`(정제·use_from·보강이 끝난 값)에서 예측기법 하나를 고른다.

    `n_observed`는 `y`의 끝에서부터 센 실제 주문 달 수다(앞쪽이 보강값이면 `y`보다 작다). None이면
    `y` 전체가 실제 값이다. `exclude`에 든 기법은 후보에서 뺀다(되돌림 재실행). 후보가 모두 제외되면 선택지가 소진된 것이라
    `forecast_method`가 None이고 `exhausted`가 true인 판단을 반환한다(③ 사람 escalation 대상).
    """
    ch = describe_series(y)
    season_length = MONTHLY_SEASON_PERIOD if ch.kind == "seasonal" else 1
    candidates = [m for m in candidate_methods(ch) if m not in (exclude or set())]
    base = {
        "characteristics": asdict(ch),
        "season_length": season_length,
        "error_metric": METHOD_ERROR_METRIC,
        "excluded": sorted(exclude or []),
    }
    if not candidates:
        return StructuredJudgment(
            judgment={**base, "forecast_method": None, "exhausted": True, "errors": {}, "holdout_months": 0},
            reasoning=f"특성 '{ch.kind}'의 후보 기법이 모두 제외돼 선택지가 소진됨 — 사람 escalation 필요",
        )

    observed = len(y) if n_observed is None else min(n_observed, len(y))
    n_eval = min(HOLDOUT_MONTHS, observed, len(y) - MIN_TRAIN_MONTHS)
    if n_eval < MIN_HOLDOUT_MONTHS:
        chosen = candidates[0]
        return StructuredJudgment(
            judgment={**base, "forecast_method": chosen, "exhausted": False, "errors": {}, "holdout_months": 0},
            reasoning=f"특성 '{ch.kind}', 실제 주문 {observed}개월(이력 {len(y)}개월)로 평가할 달이 부족해 후보 중 첫 기법 '{chosen}'을 규칙대로 선택",
            ambiguous=True,
            ambiguity_reason=f"평가할 실제 주문 달이 {max(n_eval, 0)}개(기준 {MIN_HOLDOUT_MONTHS}개 이상)로 부족해 기법을 비교하지 못함",
        )

    errors = {
        m: stats_adapter.rolling_one_step_mae(m, y.to_numpy(dtype=float), season_length, n_eval, MIN_TRAIN_MONTHS)
        for m in candidates
    }
    valid = {m: e for m, e in errors.items() if not np.isnan(e)}
    shown = {m: (None if np.isnan(e) else round(float(e), 4)) for m, e in errors.items()}
    if not valid:
        chosen = candidates[0]
        return StructuredJudgment(
            judgment={**base, "forecast_method": chosen, "exhausted": False, "errors": shown, "holdout_months": n_eval},
            reasoning=f"특성 '{ch.kind}': 모든 후보를 평가할 수 없어 첫 기법 '{chosen}'을 규칙대로 선택",
            ambiguous=True,
            ambiguity_reason="평가 가능한 후보가 없음",
        )
    ranked = sorted(valid.items(), key=lambda item: (item[1], candidates.index(item[0])))
    best, best_error = ranked[0]
    ambiguous, reason = False, None
    if len(ranked) > 1:
        second, second_error = ranked[1]
        relative = (second_error - best_error) / max(best_error, 1e-9)
        if relative <= METHOD_AMBIGUITY_REL_DIFF:
            ambiguous = True
            reason = (
                f"상위 두 기법 '{best}'({best_error:.4g})와 '{second}'({second_error:.4g})의 "
                f"MAE 상대차가 {relative:.1%}로 기준({METHOD_AMBIGUITY_REL_DIFF:.0%}) 이하"
            )
    return StructuredJudgment(
        judgment={**base, "forecast_method": best, "exhausted": False, "errors": shown, "holdout_months": n_eval},
        reasoning=(
            f"특성 '{ch.kind}'(이력 {len(y)}개월, ADI={'없음' if ch.adi is None else format(ch.adi, '.2f')}"
            + (f", 계절성 강도 {ch.seasonal_strength:.2f}" if ch.seasonal_strength is not None else "")
            + f") 후보 {candidates} 중 최근 {n_eval}개월 한 달 앞 예측 MAE가 가장 낮은 '{best}'를 선택"
        ),
        ambiguous=ambiguous,
        ambiguity_reason=reason,
    )
