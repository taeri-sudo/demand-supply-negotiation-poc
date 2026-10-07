"""forecast agent의 "가정별 요청량 예측값 계산" 단계 (함수).

"통계기법 선택"이 가정마다 고른 기법으로 다음 달 요청량을 계산한다.

1. 기법마다 요청량을 계산한다 → `method_values`(기법별 값, 기법 가중치 포함). driver의 데이터를
   반영하는 방식은 기법마다 다르다(회귀 계열만 설명변수로 받는다 — `driver_regressors.py`). 변화율·배율 같은
   중간 값을 모든 기법에 일괄 적용하지 않는다.
2. 가정 안에서 기법별 값을 **합치거나 하나 선택**해 가정의 `value`를 정한다(`combine_method_values`):
   - 기법이 하나면 그 값
   - 기법 가중치가 가장 큰 기법이 `METHOD_DOMINANT_WEIGHT` 이상이면 그 기법 하나를 선택
   - 그렇지 않으면 기법 가중치로 가중 평균
   기법별 값이 `ASSUMPTION_METHOD_SPREAD`보다 넓게 흩어져 있으면 "애매함"으로 표시한다.
3. `forecast_uncertainty`는 기법별 과거 정확도(MAE)를 기법 가중치로 가중한 값이다. 평가하지 못했으면 None이다.

맞는 기법이 하나도 없어 값을 계산할 수 없는 가정은 제외하고 이유를 `ExcludedAssumption`으로 남긴다.
반환 판단은 공통 `StructuredJudgment`다(공통 규칙 2).
"""

from dataclasses import dataclass, field

import pandas as pd

from . import stats_adapter
from .data_source_judgment import DataCollectionResult
from .driver_regressors import build_regressors
from .external_data import InstanceInputs
from .forecast_method_selection import select_methods
from .judgment import StructuredJudgment
from .judgment_thresholds import ASSUMPTION_METHOD_SPREAD, METHOD_DOMINANT_WEIGHT
from .logging_utils import log
from .state import Assumption, Driver, ExcludedAssumption, ExcludedDriver, MethodValue

ROLE_TAG = "forecast"


@dataclass
class CalculatedAssumptions:
    assumptions: list[Assumption]
    excluded_assumptions: list[ExcludedAssumption] = field(default_factory=list)
    excluded_drivers: list[ExcludedDriver] = field(default_factory=list)  # 이 단계에서 반영하지 못한 전제
    judgments: list[StructuredJudgment] = field(default_factory=list)


def combine_method_values(method_values: list[MethodValue]) -> StructuredJudgment:
    """기법별 값을 합치거나 하나 선택해 가정의 value를 정한다."""
    if len(method_values) == 1:
        only = method_values[0]
        return StructuredJudgment(
            judgment={"value": only.value, "how": "single", "methods": [only.method]},
            reasoning=f"기법이 '{only.method}' 하나라 그 값을 가정의 value로 씀",
        )
    top = max(method_values, key=lambda m: m.method_weight)
    values = [m.value for m in method_values]
    mean = sum(m.value * m.method_weight for m in method_values) / sum(m.method_weight for m in method_values)
    spread = (max(values) - min(values)) / mean if mean > 0 else 0.0
    ambiguous = spread > ASSUMPTION_METHOD_SPREAD
    reason = f"기법별 값의 흩어짐 {spread:.1%}가 기준({ASSUMPTION_METHOD_SPREAD:.0%})을 넘음" if ambiguous else None
    if top.method_weight >= METHOD_DOMINANT_WEIGHT:
        return StructuredJudgment(
            judgment={"value": top.value, "how": "selected", "methods": [top.method], "spread": spread},
            reasoning=(
                f"기법 '{top.method}'의 가중치 {top.method_weight:.2f}가 기준({METHOD_DOMINANT_WEIGHT}) 이상이라 "
                "그 기법 하나를 선택"
            ),
            ambiguous=ambiguous,
            ambiguity_reason=reason,
        )
    return StructuredJudgment(
        judgment={"value": mean, "how": "weighted_mean", "methods": [m.method for m in method_values], "spread": spread},
        reasoning=f"가중치가 가장 큰 기법의 가중치 {top.method_weight:.2f}가 기준({METHOD_DOMINANT_WEIGHT}) 미만이라 가중 평균",
        ambiguous=ambiguous,
        ambiguity_reason=reason,
    )


def _tag(judgment: StructuredJudgment, assumption_id: str) -> StructuredJudgment:
    return judgment.model_copy(update={"judgment": {"assumption_id": assumption_id, **judgment.judgment}})


def _calculate_one(
    assumption: Assumption,
    premises: list[Driver],
    inputs: InstanceInputs,
    collection: DataCollectionResult,
    planning_month: pd.Timestamp,
    exclude: set[str] | None = None,
) -> tuple[Assumption | None, list[StructuredJudgment], str]:
    """가정 하나의 기법을 고르고 값을 계산한다. 맞는 기법이 없으면 (None, 판단, 이유)를 반환한다.

    `exclude`는 send-back 재실행이 이 가정에서 제외한 기법이다.
    """
    training = collection.training_for(assumption.assumption_id)
    market = collection.evidence_series.get(("market", "category"))
    regressors = build_regressors(
        assumption.drivers, premises, inputs.orders, market, pd.DatetimeIndex(training.index), planning_month
    )
    selection = select_methods(training, collection.n_observed_months, regressors, exclude)
    judgments = [_tag(selection, assumption.assumption_id)]
    y = training.to_numpy(dtype=float)
    computed: list[tuple[str, float, float, float | None]] = []  # (기법, 값, 가중치, MAE)
    for chosen in selection.judgment["methods"]:
        try:
            value = stats_adapter.forecast_value(
                chosen["method"],
                y,
                regressors.matrix if regressors is not None else None,
                regressors.x_future if regressors is not None else None,
            )
        except Exception:  # 계산할 수 없는 기법은 이 가정에서 제외한다
            continue
        computed.append((chosen["method"], max(value, 0.0), chosen["method_weight"], chosen["mae"]))
    if not computed:
        return None, judgments, selection.reasoning
    total = sum(weight for _, _, weight, _ in computed)
    pairs = sorted(
        ((MethodValue(method=m, value=v, method_weight=w / total), mae) for m, v, w, mae in computed),
        key=lambda pair: -pair[0].method_weight,
    )
    method_values = [mv for mv, _ in pairs]
    combined = combine_method_values(method_values)
    judgments.append(_tag(combined, assumption.assumption_id))
    uncertainty = (
        None
        if any(mae is None for _, mae in pairs)
        else sum(mv.method_weight * (mae or 0.0) for mv, mae in pairs)
    )
    result = assumption.model_copy(
        update={"method_values": method_values, "value": combined.judgment["value"], "forecast_uncertainty": uncertainty}
    )
    return result, judgments, ""


def select_methods_and_calculate_values(
    assumptions: list[Assumption],
    premises: list[Driver],
    inputs: InstanceInputs,
    collection: DataCollectionResult,
    planning_month: pd.Timestamp,
    method_exclusions: dict[str, set[str]] | None = None,
) -> CalculatedAssumptions:
    """가정마다 통계기법을 고르고 기법별 값과 가정의 value·forecast_uncertainty를 채운다.

    기본 가정(`drivers`가 비어 있음)은 확정 프로모션 전제 유무와 상관없이 항상 후보에 남는다. 전제를 설명변수로
    받는 기법이 하나도 계산되지 않으면 전제를 반영하지 않고 우리 주문 이력만으로(시계열 기법 포함) 계산하고,
    전제를 반영하지 못했다는 사실과 이유를 `ExcludedDriver`로 남기며 "애매함"으로 표시한다. 원인이 있는 가정은
    맞는 기법이 없으면 제외한다. 이력이 짧아 전제 없이도 어떤 기법도 계산되지 않는 기본 가정만 제외된다. 사용할 주문이
    없어 계산할 수 없는 수집 결과면 계산하지 않고 빈 결과를 돌려준다(이유는 `collection.unusable_reason`).
    `method_exclusions`는 send-back 재실행이 가정마다 제외한 기법이다(가정 ID별, 그 가정 안에서만 적용한다).
    """
    method_exclusions = method_exclusions or {}
    if collection.unusable_reason is not None:
        log(ROLE_TAG, "select_methods_and_calculate_values", calculated=[], unusable=collection.unusable_reason)
        return CalculatedAssumptions(assumptions=[])
    calculated: list[Assumption] = []
    excluded: list[ExcludedAssumption] = []
    excluded_drivers: list[ExcludedDriver] = []
    judgments: list[StructuredJudgment] = []

    for assumption in assumptions:
        exclude = method_exclusions.get(assumption.assumption_id)
        result, found, reason = _calculate_one(assumption, premises, inputs, collection, planning_month, exclude)
        judgments.extend(found)
        if result is None and premises and not assumption.drivers:
            result, retried, retry_reason = _calculate_one(assumption, [], inputs, collection, planning_month, exclude)
            judgments.extend(retried)
            if result is not None:
                note = f"확정 프로모션 전제를 설명변수로 받는 기법이 하나도 계산되지 않아 전제를 반영하지 못함: {reason}"
                excluded_drivers.append(
                    ExcludedDriver(
                        driver="event",
                        assumption_ids=[assumption.assumption_id],
                        reason="no_applicable_method",
                        rationale=note,
                    )
                )
                judgments.append(
                    _tag(
                        StructuredJudgment(
                            judgment={"decision": "premise_not_reflected", "premises": [p.driver for p in premises]},
                            reasoning=f"{note} — 전제 없이 우리 주문 이력만으로 계산",
                            ambiguous=True,
                            ambiguity_reason="확정된 프로모션 일정을 반영하지 못한 값",
                        ),
                        assumption.assumption_id,
                    )
                )
            else:
                reason = retry_reason or reason
        if result is None:
            excluded.append(
                ExcludedAssumption(
                    assumption_id=assumption.assumption_id,
                    reason="no_applicable_method",
                    rationale=f"이 가정에 맞는 통계기법이 없음: {reason}",
                )
            )
            continue
        calculated.append(result)
    log(ROLE_TAG, "select_methods_and_calculate_values", calculated=[a.assumption_id for a in calculated],
        excluded=[e.assumption_id for e in excluded], premise_not_reflected=len(excluded_drivers))
    return CalculatedAssumptions(
        assumptions=calculated, excluded_assumptions=excluded, excluded_drivers=excluded_drivers, judgments=judgments
    )
