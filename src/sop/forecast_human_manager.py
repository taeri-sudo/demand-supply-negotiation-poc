"""`forecast->human_manager` 엣지: forecast가 사람 escalation 기록을 `escalation_records`에 만드는 경로.

escalation 기록을 만드는 경우는 세 가지뿐이고 모두 `mode: "intervention"`, `status: "open"`으로 시작한다
(AGENT_NODE_LIST.md "사람 escalation 세 경우").

- `no_computable_assumption`: 계산된 가정이 하나도 없어 요청량을 만들 수 없음(`scenario`는 `null`)
- `selection_unresolved`: 가정 선택 ③(`scenario`에 규칙이 낸 중간값)
- `options_exhausted`: send-back 전에 계산된 `scenario`가 있었는데 재실행으로 가정이 모두 제외됨. 이 모듈은
  escalation 기록을 만드는 함수가 이 reason을 받을 수 있게만 한다(재실행은 아직 구현돼 있지 않다)

같은 인스턴스·같은 `reason`의 처리되지 않은(open) escalation 기록이 있으면 새로 만들지 않는다. 처리되지 않은
escalation 기록이 있는 동안 그 인스턴스는 supply_coordination으로 요청량을 보내지 않는다(`has_open_escalation`).
사람이 처리한 뒤의 동작은 M4다. `interaction_protocol`에는 `selection_unresolved` 항목만 있고
(`selection_unresolved_protocol_entry`), 조회 키는 `(edge, escalation_trigger)`다.
"""

from typing import Literal

from .access import StateStore
from .data_source_judgment import DataCollectionResult
from .ids import now_iso
from .logging_utils import log
from .state import EscalationRecord, ExcludedAssumption, InteractionProtocol

ESCALATION_EDGE = "forecast->human_manager"
ForecastEscalationReason = Literal["no_computable_assumption", "selection_unresolved", "options_exhausted"]
OPEN_STATUS = "open"


def new_forecast_escalation(agent_id: str, reason: ForecastEscalationReason, rationale: str) -> EscalationRecord:
    """사람의 결정을 기다리는 `intervention` escalation 기록 하나를 만든다. State에 쓰는 일은 호출부가 한다."""
    return EscalationRecord(
        agent_id=agent_id,
        trigger_edge=ESCALATION_EDGE,
        reason=reason,
        rationale=rationale,
        mode="intervention",
        status=OPEN_STATUS,
    )


def has_open_escalation(records: list[EscalationRecord], agent_id: str, reason: str | None = None) -> bool:
    """인스턴스(`agent_id`)에 처리되지 않은(open) escalation 기록이 있는지. `reason`을 주면 그 reason의 기록만 본다."""
    return any(
        r.agent_id == agent_id and r.trigger_edge == ESCALATION_EDGE and r.status == OPEN_STATUS
        and (reason is None or r.reason == reason)
        for r in records
    )


def open_forecast_escalation(
    store: StateStore, role_tag: str, agent_id: str, reason: ForecastEscalationReason, rationale: str
) -> EscalationRecord | None:
    """escalation 기록을 `escalation_records`에 만든다.

    같은 인스턴스·같은 reason의 처리되지 않은(open) escalation 기록이 있으면 아무것도 하지 않고 None을 반환한다.
    """
    records = store.get_field(role_tag, "escalation_records")
    if has_open_escalation(records, agent_id, reason):
        log(role_tag, "open_forecast_escalation", agent_id=agent_id, reason=reason, duplicate=True)
        return None
    record = new_forecast_escalation(agent_id, reason, rationale)
    store.set_field(role_tag, "escalation_records", [*records, record], notify_channel="escalation_records")
    log(role_tag, "open_forecast_escalation", agent_id=agent_id, reason=reason)
    return record


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
    parts = [f"가정 '{e.assumption_id}' 제외({e.reason}): {e.rationale}" for e in excluded_assumptions]
    return "send-back 재실행으로 가정이 모두 제외됨" + (f" — {'; '.join(parts)}" if parts else "")


def selection_unresolved_protocol_entry() -> InteractionProtocol:
    """`forecast->human_manager`의 `selection_unresolved` 항목(STATE_SCHEMA.md 5번 절). 라운드가 없어 `max_rounds`가 없다."""
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


def find_protocol_entry(
    entries: list[InteractionProtocol], edge: str, escalation_trigger: str
) -> InteractionProtocol | None:
    """같은 엣지에 트리거가 여러 개일 수 있어 `(edge, escalation_trigger)`로 찾는다."""
    for entry in entries:
        if entry.edge == edge and entry.escalation_trigger == escalation_trigger:
            return entry
    return None
