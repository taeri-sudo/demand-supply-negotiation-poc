"""forecast agent의 "가정 정의" 단계 (규칙 기반, `defined_by: "rule"`).

AGENT_NODE_LIST.md의 첫 단계다. 어떤 가정(선택지)들을 계산할지와 그 가정에 필요한 근거 데이터를
**선언만** 한다. 기법별 값과 value는 비어 있고, 원인 확인은 "통계기법 선택"이, 요청량 계산은
"가정별 요청량 예측값 계산"이 한다. 시장 지표에서 가정의 내용을 정하는 자리는 지금은 규칙이 채우고,
M7에서 LLM이 외부 정보로 이어받는다(같은 반환 스키마).

가정 조합을 모두 나열하지 않고 입력 데이터가 있는 후보를 넓게 올린다. 같은 원인 조합이라도 종류나 쓰는
데이터, 통계기법 구성이 다르면 다른 가정이다(요청량이 같아도 합치지 않는다). 지금 올리는 후보:
- 기본 가정(`A-DEFAULT`): driver 없음. 기법별 값은 우리 주문 이력만으로 계산한다.
- `category_trend` 원인을 단 가정(`A-TREND`): 근거는 시장 데이터(`market` + `category`)와 우리 수요
  (`pos` + `same_item`).
- 확정된 프로모션 일정은 가정의 요소가 아니라 **모든 가정의 전제**(`premises`)다. 과거 프로모션 기록
  (`orders` + `same_item`)이 근거로 필요하다.
`price`는 매장 판매가 데이터가 없어 후보를 올리지 않는다.

가정 안의 driver 개수와 가정 선택에 올라가는 가정 개수에는 상한이 있다
(`MAX_DRIVERS_PER_ASSUMPTION`, `MAX_ASSUMPTIONS_FOR_SELECTION`). 가정 ID는 의미 있는 고정 문자열이다
(같은 입력이면 같은 결과 — 결정론).

**send-back 재실행**: `assumption` send-back 이유를 받으면 `redefine_assumptions`가 `assumption_id`의 가정을 다시 정의한다.
send-back 이유가 문제가 된 원인(driver)을 지목하지 못하므로 그 가정의 원인을 모두 제외하고(`excluded_drivers`에 send-back
이유로 기록), 원인이 없어진 가정은 수단이 없어 제외한다(`excluded_assumptions`에 send-back 이유로 기록). 원인이
처음부터 없는 기본 가정도 같다.
"""

from dataclasses import dataclass, field

from .data_source_judgment import SourceKey
from .external_data import InstanceInputs
from .favorita_mapping import FAMILY_MAPPING
from .judgment import StructuredJudgment
from .judgment_thresholds import MAX_ASSUMPTIONS_FOR_SELECTION, MAX_DRIVERS_PER_ASSUMPTION
from .logging_utils import log
from .state import (
    Assumption,
    Driver,
    Evidence,
    ExcludedAssumption,
    ExcludedDriver,
    SuspectedCause,
    send_back_reason,
)

ROLE_TAG = "forecast"
DEFAULT_ID = "A-DEFAULT"
TREND_ID = "A-TREND"


@dataclass
class AssumptionDefinition:
    assumptions: list[Assumption]
    premises: list[Driver]  # 모든 가정의 전제
    required_evidence: list[SourceKey]  # 데이터 수집에 넘길 필수 근거
    judgments: list[StructuredJudgment] = field(default_factory=list)


def required_evidence_for(assumptions: list[Assumption], premises: list[Driver]) -> list[SourceKey]:
    """가정의 원인과 전제가 필요로 하는 근거 목록. category_trend는 시장 데이터와 우리 수요(POS)가 필요하다."""
    required: list[SourceKey] = []
    for assumption in assumptions:
        for driver in assumption.drivers:
            if driver.driver == "category_trend":
                required.append(("pos", "same_item"))
            required.append((driver.evidence.kind, driver.evidence.item_scope))
    required.extend((p.evidence.kind, p.evidence.item_scope) for p in premises)
    return list(dict.fromkeys(required))


def define_assumptions(inputs: InstanceInputs, scheduled_promotion: bool = False) -> AssumptionDefinition:
    """가정과 필요한 근거를 선언한다. 데이터를 읽거나 값을 계산하지 않는다."""
    premises: list[Driver] = []
    if scheduled_promotion:  # event: 과거 프로모션 기록이 근거
        premises.append(
            Driver(
                driver="event",
                evidence=Evidence(kind="orders", item_scope="same_item", refs=[inputs.item_id]),
            )
        )
    groups = list(FAMILY_MAPPING[inputs.family].market_groups)
    trend = Driver(
        driver="category_trend", evidence=Evidence(kind="market", item_scope="category", refs=groups)
    )
    assumptions = [
        Assumption(assumption_id=DEFAULT_ID),
        Assumption(assumption_id=TREND_ID, drivers=[trend]),
    ]
    if any(len(a.drivers) > MAX_DRIVERS_PER_ASSUMPTION for a in assumptions):
        raise ValueError(f"가정 하나 안의 driver는 {MAX_DRIVERS_PER_ASSUMPTION}개 이하여야 함")
    assumptions = assumptions[:MAX_ASSUMPTIONS_FOR_SELECTION]
    required = required_evidence_for(assumptions, premises)
    judgment = StructuredJudgment(
        judgment={
            "decision": "assumptions_defined",
            "assumptions": [a.assumption_id for a in assumptions],
            "premises": [p.driver for p in premises],
            "required_evidence": [f"{k[0]}/{k[1]}" for k in required],
        },
        reasoning=(
            "기본 가정과 category_trend 원인을 단 가정을 선언"
            + (", 예정된 프로모션을 모든 가정의 전제로 선언" if scheduled_promotion else "")
            + " — 기법별 값과 요청량은 통계기법 선택과 가정별 요청량 예측값 계산 단계에서 채움"
        ),
    )
    log(ROLE_TAG, "define_assumptions", company_id=inputs.company_id, item_id=inputs.item_id,
        assumptions=[a.assumption_id for a in assumptions], required=len(required))
    return AssumptionDefinition(
        assumptions=assumptions, premises=premises, required_evidence=required, judgments=[judgment]
    )


@dataclass
class Redefinition:
    definition: AssumptionDefinition
    excluded_assumptions: list[ExcludedAssumption]  # 원인이 없어져 수단이 없는 가정(send-back 이유를 제외 이유로 기록)
    excluded_drivers: list[ExcludedDriver]  # 제외한 원인(send-back 이유를 제외 이유로 기록)


def redefine_assumptions(definition: AssumptionDefinition, cause: SuspectedCause) -> Redefinition:
    """`assumption` send-back 이유를 받아 `assumption_id`의 가정을 다시 정의한다.

    send-back 이유가 문제가 된 원인을 지목하지 못하므로 그 가정의 원인을 모두 제외한다. 원인이 없어진 가정(원인이 처음부터
    없는 기본 가정 포함)은 기본 가정과 같은 구성이거나 뺄 원인이 없어 대응할 수단이 없으므로 제외한다. 다시 정의한
    결과의 필요한 근거 목록도 새로 만든다.
    """
    if cause.type != "assumption" or cause.assumption_id is None:
        raise ValueError("assumption send-back 이유에는 assumption_id가 필요함")
    target = next((a for a in definition.assumptions if a.assumption_id == cause.assumption_id), None)
    if target is None:
        raise ValueError(f"send-back 이유가 가리키는 가정 {cause.assumption_id!r}이 정의에 없음")

    reason = send_back_reason(cause)
    excluded_drivers = [
        ExcludedDriver(
            driver=d.driver,
            assumption_ids=[target.assumption_id],
            reason=reason,
            rationale=f"{reason} send-back이 원인을 지목하지 못해 가정 '{target.assumption_id}'의 원인을 모두 제외",
        )
        for d in target.drivers
    ]
    note = (
        f"가정 '{target.assumption_id}'은 원인이 없는 기본 가정이라 제외할 원인이 없어 수단이 없음"
        if not target.drivers
        else f"가정 '{target.assumption_id}'의 원인 {[d.driver for d in target.drivers]}을 모두 제외하면 원인이 없어 수단이 없음"
    )
    excluded_assumptions = [ExcludedAssumption(assumption_id=target.assumption_id, reason=reason, rationale=note)]
    kept = [a for a in definition.assumptions if a.assumption_id != target.assumption_id]
    premises = list(definition.premises)
    judgment = StructuredJudgment(
        judgment={"decision": "assumption_redefined", "assumption_id": target.assumption_id, "send_back": reason,
                  "excluded_drivers": [d.driver for d in target.drivers], "excluded": True},
        reasoning=note,
    )
    log(ROLE_TAG, "redefine_assumptions", assumption_id=target.assumption_id, send_back=reason,
        kept=[a.assumption_id for a in kept])
    return Redefinition(
        definition=AssumptionDefinition(
            assumptions=kept, premises=premises, required_evidence=required_evidence_for(kept, premises),
            judgments=[*definition.judgments, judgment],
        ),
        excluded_assumptions=excluded_assumptions,
        excluded_drivers=excluded_drivers,
    )
