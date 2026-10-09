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

**재실행**: `assumption` 의심되는 원인을 받으면 `redefine_assumptions`가 `assumption_id`의 가정을 다시 정의한다.
`drivers`가 있으면 지목한 원인만, null이면 그 가정의 모든 원인을 제외하고(`excluded_drivers`에 의심되는 원인으로 기록),
원인이 하나도 남지 않은 가정은 수단이 없어 제외한다(`excluded_assumptions`에 의심되는 원인으로 기록). 원인이 처음부터
없는 기본 가정도 같다. 한 가정에 이유가 여러 개면 모두 적용한다.
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
    suspected_cause_reason,
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
    excluded_assumptions: list[ExcludedAssumption]  # 원인이 없어져 수단이 없는 가정(의심되는 원인을 제외 이유로 기록)
    excluded_drivers: list[ExcludedDriver]  # 제외한 원인(의심되는 원인을 제외 이유로 기록)


def _covers(cause: SuspectedCause, driver: Driver) -> bool:
    """의심되는 원인이 이 원인을 제외하라고 하는가. `drivers`가 null이면 그 가정의 모든 원인이다."""
    return cause.drivers is None or driver.driver in cause.drivers


def redefine_assumptions(definition: AssumptionDefinition, causes: list[SuspectedCause]) -> Redefinition:
    """`assumption` 의심되는 원인들을 받아 `assumption_id`의 가정을 다시 정의한다.

    이유의 `drivers`가 있으면 지목한 원인만, null이면 그 가정의 모든 원인을 제외한다. 한 원인이나 가정을 제외하게 한 이유가 여럿이면
    모두 제외 이유 목록에 기록한다. 원인이 하나도 남지 않은 가정(원인이 처음부터 없는 기본 가정 포함)은
    기본 가정과 같은 구성이거나 뺄 원인이 없어 대응할 수단이 없으므로 제외한다. 다시 정의한 결과의 필요한 근거 목록도
    새로 만든다.
    """
    by_assumption: dict[str, list[SuspectedCause]] = {}
    for cause in causes:
        if cause.type != "assumption" or cause.assumption_id is None:
            raise ValueError("assumption 의심되는 원인에는 assumption_id가 필요함")
        by_assumption.setdefault(cause.assumption_id, []).append(cause)
    known = {a.assumption_id for a in definition.assumptions}
    for assumption_id in by_assumption:
        if assumption_id not in known:
            raise ValueError(f"의심되는 원인이 가리키는 가정 {assumption_id!r}이 정의에 없음")

    excluded_drivers: list[ExcludedDriver] = []
    excluded_assumptions: list[ExcludedAssumption] = []
    kept: list[Assumption] = []
    judgments: list[StructuredJudgment] = []
    for assumption in definition.assumptions:
        own = by_assumption.get(assumption.assumption_id)
        if own is None:
            kept.append(assumption)
            continue
        remaining: list[Driver] = []
        removed: list[Driver] = []
        for driver in assumption.drivers:
            first = next((c for c in own if _covers(c, driver)), None)
            if first is None:
                remaining.append(driver)
                continue
            removed.append(driver)
            covering = list(dict.fromkeys(suspected_cause_reason(c) for c in own if _covers(c, driver)))
            how = "지목한 원인" if first.drivers is not None else "원인을 지목하지 못해 모든 원인"
            excluded_drivers.append(
                ExcludedDriver(
                    driver=driver.driver,
                    assumption_ids=[assumption.assumption_id],
                    reasons=covering,
                    rationale=f"{', '.join(covering)} 의심되는 원인이 {how}으로 가정 '{assumption.assumption_id}'에서 제외",
                )
            )
        reasons = list(dict.fromkeys(suspected_cause_reason(c) for c in own))
        if remaining:
            kept.append(assumption.model_copy(update={"drivers": remaining}))
            note = f"가정 '{assumption.assumption_id}'에서 원인 {[d.driver for d in removed]}을 제외하고 다시 정의"
        else:
            if not assumption.drivers:
                note = f"가정 '{assumption.assumption_id}'은 원인이 없는 기본 가정이라 제외할 원인이 없어 수단이 없음"
            else:
                note = f"가정 '{assumption.assumption_id}'의 원인 {[d.driver for d in assumption.drivers]}을 모두 제외하면 원인이 없어 수단이 없음"
            excluded_assumptions.append(
                ExcludedAssumption(assumption_id=assumption.assumption_id, reasons=reasons, rationale=note)
            )
        judgments.append(
            StructuredJudgment(
                judgment={"decision": "assumption_redefined", "assumption_id": assumption.assumption_id,
                          "send_back": reasons, "excluded_drivers": [d.driver for d in removed],
                          "excluded": not remaining},
                reasoning=note,
            )
        )
    premises = list(definition.premises)
    log(ROLE_TAG, "redefine_assumptions", assumption_ids=list(by_assumption), causes=[suspected_cause_reason(c) for c in causes],
        kept=[a.assumption_id for a in kept])
    return Redefinition(
        definition=AssumptionDefinition(
            assumptions=kept, premises=premises, required_evidence=required_evidence_for(kept, premises),
            judgments=[*definition.judgments, *judgments],
        ),
        excluded_assumptions=excluded_assumptions,
        excluded_drivers=excluded_drivers,
    )
