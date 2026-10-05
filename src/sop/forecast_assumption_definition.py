"""forecast agent의 "가정 정의" 단계 (규칙 기반, `defined_by: "rule"`).

AGENT_NODE_LIST.md의 첫 단계다. 어떤 가정(선택지)들을 계산할지와 그 가정에 필요한 근거 데이터를
**선언만** 한다. 기법별 값과 value는 비어 있고, 요인 확인은 "통계기법 선택"이, 요청량 계산은
"가정별 요청량 예측값 계산"이 한다. 시장 지표에서 가정의 내용을 정하는 자리는 지금은 규칙이 채우고,
M7에서 LLM이 외부 정보로 이어받는다(같은 반환 스키마).

가정 조합을 모두 나열하지 않고 입력 데이터가 있는 후보를 넓게 올린다. 같은 요인 조합이라도 종류나 쓰는
데이터, 통계기법 구성이 다르면 다른 가정이다(요청량이 같아도 합치지 않는다). 지금 올리는 후보:
- 기본 가정(`A-DEFAULT`): driver 없음. 기법별 값은 우리 주문 이력만으로 계산한다.
- `category_trend` 요인을 단 가정(`A-TREND`): 근거는 시장 데이터(`market` + `category`)와 우리 수요
  (`pos` + `same_item`).
- 확정된 프로모션 일정은 가정의 요소가 아니라 **모든 가정의 전제**(`premises`)다. 과거 프로모션 기록
  (`orders` + `same_item`)이 근거로 필요하다.
`price`는 매장 판매가 데이터가 없어 후보를 올리지 않는다.

가정 안의 driver 개수와 가정 선택에 올라가는 가정 개수에는 상한이 있다
(`MAX_DRIVERS_PER_ASSUMPTION`, `MAX_ASSUMPTIONS_FOR_SELECTION`). 가정 ID는 의미 있는 고정 문자열이다
(같은 입력이면 같은 결과 — 결정론).
"""

from dataclasses import dataclass, field

from .data_source_judgment import SourceKey
from .external_data import InstanceInputs
from .favorita_mapping import FAMILY_MAPPING
from .judgment import StructuredJudgment
from .judgment_thresholds import MAX_ASSUMPTIONS_FOR_SELECTION, MAX_DRIVERS_PER_ASSUMPTION
from .logging_utils import log
from .state import Assumption, Driver, Evidence

ROLE_TAG = "forecast"
DEFAULT_ID = "A-DEFAULT"
TREND_ID = "A-TREND"


@dataclass
class AssumptionDefinition:
    assumptions: list[Assumption]
    premises: list[Driver]  # 모든 가정의 전제
    required_evidence: list[SourceKey]  # 데이터 수집에 넘길 필수 근거
    judgments: list[StructuredJudgment] = field(default_factory=list)


def define_assumptions(inputs: InstanceInputs, scheduled_promotion: bool = False) -> AssumptionDefinition:
    """가정과 필요한 근거를 선언한다. 데이터를 읽거나 값을 계산하지 않는다."""
    required: list[SourceKey] = [("pos", "same_item"), ("market", "category")]  # category_trend
    premises: list[Driver] = []
    if scheduled_promotion:
        required.append(("orders", "same_item"))  # event: 과거 프로모션 기록
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
    required = list(dict.fromkeys(required))
    judgment = StructuredJudgment(
        judgment={
            "decision": "assumptions_defined",
            "assumptions": [a.assumption_id for a in assumptions],
            "premises": [p.driver for p in premises],
            "required_evidence": [f"{k[0]}/{k[1]}" for k in required],
        },
        reasoning=(
            "기본 가정과 category_trend 요인을 단 가정을 선언"
            + (", 예정된 프로모션을 모든 가정의 전제로 선언" if scheduled_promotion else "")
            + " — 기법별 값과 요청량은 통계기법 선택과 가정별 요청량 예측값 계산 단계에서 채움"
        ),
    )
    log(ROLE_TAG, "define_assumptions", company_id=inputs.company_id, item_id=inputs.item_id,
        assumptions=[a.assumption_id for a in assumptions], required=len(required))
    return AssumptionDefinition(
        assumptions=assumptions, premises=premises, required_evidence=required, judgments=[judgment]
    )
