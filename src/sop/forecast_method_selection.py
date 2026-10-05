"""forecast agent의 "통계기법 선택" 단계 (규칙 기반).

가정마다 계산에 쓸 통계기법을 여러 개 고르고 기법 가중치를 정한다. 규칙은 기법을 확정하지 않는다.

1. **후보를 넓게 올린다**: 기본 후보(지수평활, 이동평균, 전체 평균, 직전값, 회귀, AR(1) 오차 회귀)에
   데이터 특성에 따라 후보를 **추가**한다 — 간헐수요(평균 수요 간격 ADI가 `INTERMITTENT_ADI` 이상)면
   Croston 계열, 강한 계절성(STL 기반 강도가 `SEASONAL_STRENGTH_MIN` 이상)이면 계절 기법. 특성이 후보를
   없애지는 않는다.
2. **반영할 수 없는 기법만 제외한다**: 가정에 driver나 전제가 있으면(설명변수가 있으면) driver를
   반영할 수 있는 회귀 계열만 남고 시계열만 쓰는 기법은 제외한다. 계산할 수 없는 기법(이력 부족 등)도
   제외한다. 맞는 기법이 하나도 없으면 호출부가 그 가정을 제외하고 이유를 기록한다.
3. **후보를 모두 계산해서 가정마다 따로 잰 과거 정확도를 기법 가중치로 쓴다**: 그 가정이 성립했던
   기간(실제 주문이 있고 설명변수가 있는 달)의 마지막 `HOLDOUT_MONTHS`개월을 한 달 앞 예측으로
   평가한다(walk-forward, 오차는 MAE). 가중치는 오차 역제곱을 정규화한 값이고, 가정마다 상위
   `MAX_METHODS_PER_ASSUMPTION`개만 남긴다. 모든 기법에 같은 보정값을 일괄 적용하지 않는다.
4. **표본이 부족하면** 평가할 달이 `MIN_HOLDOUT_MONTHS`개월 미만이면 평가하지 않고 기법 가중치를 균등으로
   두며 "애매함"으로 표시한다. 상위 두 기법의 MAE 상대차가 `METHOD_AMBIGUITY_REL_DIFF` 이하여도
   "애매함"이다.

되돌림으로 재실행될 때는 `exclude`에 직전 기법을 넘겨 제외한 채 다시 고른다. 반환은 공통
`StructuredJudgment`다(공통 규칙 2).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from . import stats_adapter
from .driver_regressors import Regressors
from .judgment import StructuredJudgment
from .judgment_thresholds import (
    HOLDOUT_MONTHS,
    INTERMITTENT_ADI,
    MAX_METHODS_PER_ASSUMPTION,
    METHOD_AMBIGUITY_REL_DIFF,
    METHOD_ERROR_FLOOR_REL,
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
    intermittent: bool
    strong_seasonal: bool


def describe_series(y: pd.Series) -> SeriesCharacteristics:
    values = y.to_numpy(dtype=float)
    n = len(values)
    nonzero = int((values > 0).sum())
    adi = n / nonzero if nonzero else None
    strength = (
        stats_adapter.seasonal_strength(values, MONTHLY_SEASON_PERIOD) if n >= MIN_HISTORY_MONTHS else None
    )
    return SeriesCharacteristics(
        n_months=n,
        zero_fraction=float((values == 0).mean()) if n else 1.0,
        adi=adi,
        seasonal_strength=strength,
        intermittent=adi is None or adi >= INTERMITTENT_ADI,
        strong_seasonal=strength is not None and strength >= SEASONAL_STRENGTH_MIN,
    )


BASE_CANDIDATES = ["ets", "window_average", "historic_average", "naive", "regression", "regression_ar1"]


def candidate_methods(characteristics: SeriesCharacteristics) -> list[str]:
    """기본 후보에 데이터 특성이 요구하는 후보를 추가한다(특성은 후보를 없애지 않는다)."""
    candidates = list(BASE_CANDIDATES)
    if characteristics.intermittent:
        candidates.append("croston_sba")
    if characteristics.strong_seasonal:
        candidates += ["ets_seasonal", "seasonal_naive"]
    return candidates


def _origins(y: np.ndarray, n_observed: int, regressors: Regressors | None) -> list[int]:
    """평가 기준 시점: 실제 주문이 있고 설명변수가 있는 달 중 마지막 `HOLDOUT_MONTHS`개."""
    n = len(y)
    usable = []
    for i in range(max(n - n_observed, MIN_TRAIN_MONTHS), n):
        if not np.isfinite(y[i]):
            continue
        if regressors is not None and not np.isfinite(regressors.matrix[i]).all():
            continue
        usable.append(i)
    return usable[-HOLDOUT_MONTHS:]


def _method_entry(method: str, weight: float, mae: float | None, n_months: int) -> dict:
    return {
        "method": method,
        "method_weight": weight,
        "mae": None if mae is None else round(float(mae), 4),
        "season_length": stats_adapter.season_length_for(method, n_months),
    }


def select_methods(
    y: pd.Series,
    n_observed: int | None = None,
    regressors: Regressors | None = None,
    exclude: set[str] | None = None,
) -> StructuredJudgment:
    """월별 시계열 `y`(정제·use_from·보강이 끝난 값)로 가정 하나가 쓸 통계기법과 기법 가중치를 정한다.

    `n_observed`는 `y`의 끝에서부터 센 실제 주문 달 수다(앞쪽이 보강값이면 `y`보다 작다). None이면
    `y` 전체가 실제 값이다. `regressors`는 가정의 driver와 전제에서 만든 설명변수다. 맞는 기법이 하나도
    없으면 `methods`가 빈 목록인 판단을 반환한다(`exhausted`는 후보가 모두 `exclude`에 든 경우).
    """
    values = y.to_numpy(dtype=float)
    ch = describe_series(y)
    excluded_by_caller = set(exclude or [])
    candidates = [m for m in candidate_methods(ch) if m not in excluded_by_caller]
    excluded: dict[str, str] = {m: "재실행에서 제외" for m in excluded_by_caller}
    has_regressors = regressors is not None and regressors.matrix.shape[1] > 0
    if has_regressors:
        for method in [m for m in candidates if m not in stats_adapter.REGRESSION_METHODS]:
            excluded[method] = "driver를 반영할 수 없는 기법"
        candidates = [m for m in candidates if m in stats_adapter.REGRESSION_METHODS]

    base = {
        "characteristics": {
            "n_months": ch.n_months,
            "adi": ch.adi,
            "seasonal_strength": ch.seasonal_strength,
            "intermittent": ch.intermittent,
            "strong_seasonal": ch.strong_seasonal,
        },
        "error_metric": METHOD_ERROR_METRIC,
        "regressors": regressors.names if regressors is not None else [],
    }
    if not candidates:
        return StructuredJudgment(
            judgment={**base, "methods": [], "excluded": excluded, "holdout_months": 0,
                      "exhausted": bool(excluded_by_caller), "weights": "none"},
            reasoning="driver를 반영하면서 계산할 수 있는 후보 기법이 없음",
        )

    observed = len(values) if n_observed is None else min(n_observed, len(values))
    matrix = regressors.matrix if has_regressors and regressors is not None else None
    origins = _origins(values, observed, regressors if has_regressors else None)

    if len(origins) < MIN_HOLDOUT_MONTHS:
        kept = candidates[:MAX_METHODS_PER_ASSUMPTION]
        for method in candidates[MAX_METHODS_PER_ASSUMPTION:]:
            excluded[method] = "기법 개수 상한 초과"
        weight = 1.0 / len(kept)
        return StructuredJudgment(
            judgment={**base, "methods": [_method_entry(m, weight, None, ch.n_months) for m in kept],
                      "excluded": excluded, "holdout_months": len(origins), "exhausted": False,
                      "weights": "uniform"},
            reasoning=(
                f"그 가정이 성립했던 기간에서 평가할 달이 {len(origins)}개(기준 {MIN_HOLDOUT_MONTHS}개 이상)로 "
                f"부족해 기법 {kept}의 가중치를 균등({weight:.2f})으로 둠"
            ),
            ambiguous=True,
            ambiguity_reason=f"평가할 달이 {len(origins)}개로 부족해 기법 가중치를 정확도로 구하지 못함",
        )

    maes: dict[str, float] = {}
    for method in candidates:
        mae = stats_adapter.walk_forward_mae(method, values, origins, matrix)
        if np.isnan(mae):
            excluded[method] = "평가 불가(계산할 수 없는 시점이 있음)"
        else:
            maes[method] = mae
    if not maes:
        return StructuredJudgment(
            judgment={**base, "methods": [], "excluded": excluded, "holdout_months": len(origins),
                      "exhausted": False, "weights": "none"},
            reasoning="후보 기법을 모두 평가할 수 없어 이 가정에 맞는 기법이 없음",
        )

    ranked = sorted(maes.items(), key=lambda item: (item[1], candidates.index(item[0])))
    for method, _ in ranked[MAX_METHODS_PER_ASSUMPTION:]:
        excluded[method] = "기법 개수 상한 초과(정확도 순위 밖)"
    ranked = ranked[:MAX_METHODS_PER_ASSUMPTION]
    floor = METHOD_ERROR_FLOOR_REL * float(np.mean(values[origins]))
    raw = np.array([1.0 / max(mae, floor, 1e-12) ** 2 for _, mae in ranked])
    weights = raw / raw.sum()

    ambiguous, reason = False, None
    if len(ranked) > 1:
        (best, best_mae), (second, second_mae) = ranked[0], ranked[1]
        relative = (second_mae - best_mae) / max(best_mae, 1e-9)
        if relative <= METHOD_AMBIGUITY_REL_DIFF:
            ambiguous = True
            reason = (
                f"상위 두 기법 '{best}'({best_mae:.4g})와 '{second}'({second_mae:.4g})의 "
                f"MAE 상대차가 {relative:.1%}로 기준({METHOD_AMBIGUITY_REL_DIFF:.0%}) 이하"
            )
    methods = [_method_entry(m, float(w), mae, ch.n_months) for (m, mae), w in zip(ranked, weights)]
    return StructuredJudgment(
        judgment={**base, "methods": methods, "excluded": excluded, "holdout_months": len(origins),
                  "exhausted": False, "weights": "walk_forward"},
        reasoning=(
            f"후보 {candidates} 중 최근 {len(origins)}개월 한 달 앞 예측 MAE가 낮은 "
            f"{[m['method'] for m in methods]}를 남기고 오차 역제곱으로 기법 가중치를 정함"
        ),
        ambiguous=ambiguous,
        ambiguity_reason=reason,
    )
