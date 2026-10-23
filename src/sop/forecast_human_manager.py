"""forecast가 사람 escalation 기록을 `escalation_records`에 만드는 경로(`forecast->human_manager`, `forecast->system_operator`).

escalation 기록을 만드는 경우는 여섯 가지뿐이고 모두 `mode: "intervention"`, `status: "open"`으로 시작한다. 담당은 reason 대응표
(`escalation_records.py`)가 정한다(AGENT_NODE_LIST.md "사람 escalation 세 경우", "다음 agent 선택 escalation 두 경우", "실행 실패").

- `no_computable_assumption`: 계산된 가정이 하나도 없어 요청량을 만들 수 없음(`scenario`는 `null`)
- `selection_unresolved`: 가정 선택 ③(`scenario`에 규칙이 낸 중간값)
- `options_exhausted`: 재실행 전에 계산된 `scenario`가 있었는데 재실행으로 가정이 모두 제외됨
- `forecast_run_error`(system_operator): forecast 실행(첫 실행·재실행)이 예외로 끝남(기록은 이전 값 그대로)
- `no_next_agent`(system_operator): `passed` 뒤에 넘길 후보 agent가 없음(기록의 값은 그대로, `forward_to` 없음)
- `multiple_next_agents`(system_operator): 후보가 여럿이라 고르지 못함(M7 전까지)

처리되지 않은 escalation 기록이 있는 인스턴스는 "업무 대기" 또는 "시스템 대기" 상태라 실행하지 않고, 검증agent도 supply_coordination도 그
기록에 일하지 않는다(`record_state.py`). `interaction_protocol`에는 `selection_unresolved` 항목만 있다
(`selection_unresolved_protocol_entry`).
"""

from typing import Literal

from .access import StateStore
from .data_source_judgment import DataCollectionResult
from .escalation_records import OPEN_STATUS, new_escalation
from .ids import now_iso
from .state import EscalationRecord, ExcludedAssumption, InteractionProtocol

FORECAST_ROLE_TAG = "forecast"
ESCALATION_EDGE = "forecast->human_manager"  # `selection_unresolved` 항목의 엣지
ForecastEscalationReason = Literal[
    "no_computable_assumption", "selection_unresolved", "options_exhausted", "forecast_run_error",
    "no_next_agent", "multiple_next_agents",
]


def new_forecast_escalation(
    agent_id: str, reason: ForecastEscalationReason, rationale: str, log_seq: int | None = None
) -> EscalationRecord:
    """사람의 결정을 기다리는 `intervention` escalation 기록 하나를 만든다. 담당은 reason 대응표가 정한다. State에 쓰는 일은 호출부가 한다."""
    return new_escalation(FORECAST_ROLE_TAG, reason, rationale, agent_id=agent_id, log_seq=log_seq)


def has_open_escalation(records: list[EscalationRecord], agent_id: str, reason: str | None = None) -> bool:
    """forecast가 만든 처리되지 않은(open) escalation 기록이 인스턴스(`agent_id`)에 있는지. `reason`을 주면 그 reason의 기록만 본다."""
    return any(
        r.agent_id == agent_id and r.trigger_edge.startswith(f"{FORECAST_ROLE_TAG}->") and r.status == OPEN_STATUS
        and (reason is None or r.reason == reason)
        for r in records
    )


def no_computable_rationale(
    collection: DataCollectionResult | None = None, excluded_assumptions: list[ExcludedAssumption] | None = None
) -> str:
    """요청량을 만들 수 없는 이유를 사람이 읽는 설명으로 모은다."""
    parts: list[str] = []
    if collection is not None:
        if collection.unusable_reason is not None:
            parts.append(f"사용할 주문이 없음: {collection.unusable_reason}")
        elif collection.needs_human and collection.human_reason:
            parts.append(f"이력 부족 표시: {collection.human_reason}")
    parts.extend(f"가정 '{e.assumption_id}' 제외: {e.rationale}" for e in excluded_assumptions or [])
    return "; ".join(parts) if parts else "계산된 가정이 하나도 없어 요청량을 만들 수 없음"


def options_exhausted_rationale(excluded_assumptions: list[ExcludedAssumption]) -> str:
    """재실행으로 가정이 모두 제외돼 요청량을 더 만들 수 없는 이유를 사람이 읽는 설명으로 모은다."""
    parts = [f"가정 '{e.assumption_id}' 제외({', '.join(e.reasons)}): {e.rationale}" for e in excluded_assumptions]
    return "재실행으로 가정이 모두 제외됨" + (f" — {'; '.join(parts)}" if parts else "")


def selection_unresolved_protocol_entry() -> InteractionProtocol:
    """`forecast->human_manager`의 `selection_unresolved` 항목(STATE_SCHEMA.md 5번 절)."""
    return InteractionProtocol(
        edge=ESCALATION_EDGE,
        scope=["forecast", "human_manager"],
        escalation_trigger="selection_unresolved",
        escalation_target="human_manager",
        escalation_kind="rule",
        escalation_mode="intervention",
        source="initial_design",
        last_updated=now_iso(),
    )
