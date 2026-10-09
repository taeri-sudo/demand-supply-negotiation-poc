"""forecast agent "통계기법 선택"의 첫 판단 — 원인 확인(원인의 근거가 있는지).

가정 정의가 선언한 원인마다 수집된 근거로 효과가 있는지 확인하고, 없는 원인을 단 가정을 제외한다.
원인의 효과 크기는 여기서 정하지 않는다(요청량은 통계기법이 계산한다).

- `category_trend`: 시장 변화율이 우리 수요 변화율에 주는 영향을 통계로 추정해(`category_trend.py`)
  신뢰구간이 0을 포함하면 `no_significant_effect`, 근거를 수집하지 못했거나 데이터가 모자라면
  `no_evidence`로 이 원인을 단 가정을 제외한다.
- `event`(모든 가정의 전제): 기록된 프로모션 달과 기록된 비프로모션 달이 각각 충분해야 한다
  (`EVENT_MIN_PROMO_MONTHS`, `EVENT_MIN_NON_PROMO_MONTHS`). 부족하면 가정을 제외하지 않고 전제만
  모든 가정에서 제외한다. 프로모션 효과는 기록 있는 구간으로만 센다(기록 없음은 세지 않는다).

제외한 원인과 이유, 영향받은 가정은 `ExcludedDriver`로 State에 남긴다.
"""

from dataclasses import dataclass, field

import pandas as pd

from .category_trend import estimate_category_trend
from .data_source_judgment import DataCollectionResult
from .external_data import InstanceInputs
from .judgment import StructuredJudgment
from .judgment_thresholds import EVENT_MIN_NON_PROMO_MONTHS, EVENT_MIN_PROMO_MONTHS
from .logging_utils import log
from .state import Assumption, Driver, DriverName, ExcludedDriver

ROLE_TAG = "forecast"


@dataclass
class DriverCheck:
    assumptions: list[Assumption]  # 원인 확인을 통과한 가정
    premises: list[Driver]  # 근거가 있는 전제
    excluded_drivers: list[ExcludedDriver] = field(default_factory=list)
    judgments: list[StructuredJudgment] = field(default_factory=list)


def _monthly_promotion_states(orders: pd.DataFrame, start: pd.Timestamp, last_month: pd.Timestamp) -> list[bool | None]:
    """월별 프로모션 상태(True 있음 / False 없음 / None 기록 없음). 주문이 있는 달만 센다."""
    frame = orders.assign(month=orders["order_date"].dt.to_period("M").dt.to_timestamp())
    frame = frame[(frame["month"] >= start) & (frame["month"] <= last_month)]
    states: list[bool | None] = []
    for _, group in frame.groupby("month"):
        promo = group["promotion"]
        states.append(True if promo.eq(True).any() else (False if promo.notna().all() else None))
    return states


def check_event_records(orders: pd.DataFrame, start: pd.Timestamp, last_month: pd.Timestamp) -> StructuredJudgment:
    """과거 프로모션 기록이 전제를 쓰기에 충분한지 확인한다."""
    states = _monthly_promotion_states(orders, start, last_month)
    promo, normal, unrecorded = states.count(True), states.count(False), states.count(None)
    counts = {"promo_months": promo, "non_promo_months": normal, "unrecorded_months": unrecorded}
    if promo < EVENT_MIN_PROMO_MONTHS or normal < EVENT_MIN_NON_PROMO_MONTHS:
        return StructuredJudgment(
            judgment={"decision": "no_evidence", **counts},
            reasoning=(
                f"event 전제 근거 없음: 기록된 프로모션 {promo}개월(기준 {EVENT_MIN_PROMO_MONTHS}개월 이상), "
                f"기록된 비프로모션 {normal}개월(기준 {EVENT_MIN_NON_PROMO_MONTHS}개월 이상)"
            ),
        )
    return StructuredJudgment(
        judgment={"decision": "sufficient_records", **counts},
        reasoning=f"기록된 프로모션 {promo}개월, 비프로모션 {normal}개월로 전제를 쓸 수 있음(기록 없음 {unrecorded}개월은 제외)",
    )


def check_drivers(
    assumptions: list[Assumption],
    premises: list[Driver],
    inputs: InstanceInputs,
    collection: DataCollectionResult,
    planning_month: pd.Timestamp,
) -> DriverCheck:
    """원인마다 근거를 확인하고, 근거가 없는 원인을 단 가정과 근거가 없는 전제를 제외한다.

    사용할 주문이 없어 계산할 수 없는 수집 결과면 확인하지 않고 입력을 그대로 돌려준다.
    """
    if collection.unusable_reason is not None or collection.observed_start is None:
        return DriverCheck(assumptions=list(assumptions), premises=list(premises))
    judgments: list[StructuredJudgment] = []
    failed: dict[DriverName, StructuredJudgment] = {}

    if any(p.driver == "event" for p in premises):
        event = check_event_records(inputs.orders, collection.observed_start, planning_month)
        judgments.append(event)
        if event.judgment["decision"] != "sufficient_records":
            failed["event"] = event

    if any(d.driver == "category_trend" for a in assumptions for d in a.drivers):
        demand = collection.evidence_series.get(("pos", "same_item"))
        market = collection.evidence_series.get(("market", "category"))
        if isinstance(demand, pd.Series) and market is not None:
            trend = estimate_category_trend(demand, market, planning_month)
        else:
            missing = [f"{k[0]}/{k[1]}" for k in (("pos", "same_item"), ("market", "category")) if k not in collection.evidence_series]
            trend = StructuredJudgment(
                judgment={"decision": "no_evidence", "unavailable": missing},
                reasoning=f"category_trend 원인 근거 없음: {missing} 수집 불가",
            )
        judgments.append(trend)
        if trend.judgment["decision"] != "significant_effect":
            failed["category_trend"] = trend

    excluded: list[ExcludedDriver] = []
    for driver, decision in failed.items():
        affected = (
            [a.assumption_id for a in assumptions]
            if driver == "event"
            else [a.assumption_id for a in assumptions if any(d.driver == driver for d in a.drivers)]
        )
        excluded.append(
            ExcludedDriver(
                driver=driver,
                assumption_ids=affected,
                reasons=[decision.judgment["decision"]],
                rationale=decision.reasoning,
            )
        )
    kept = [a for a in assumptions if not any(d.driver in failed for d in a.drivers)]
    kept_premises = [p for p in premises if p.driver not in failed]
    log(ROLE_TAG, "check_drivers", kept=[a.assumption_id for a in kept],
        excluded=[(e.driver, e.reasons) for e in excluded])
    return DriverCheck(
        assumptions=kept, premises=kept_premises, excluded_drivers=excluded, judgments=judgments
    )
