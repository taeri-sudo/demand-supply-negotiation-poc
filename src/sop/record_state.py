"""기록의 상태와 신호 규칙 (GRAPH_FLOW.md "신호 규칙").

기록의 상태는 이미 있는 값에서 계산하며 따로 저장하지 않는다. State 갱신으로 기록의 상태가 바뀌면 `access.StateStore.update_state`가 그
상태의 담당에게 자동으로 신호를 보낸다. 받는 쪽은 신호를 받으면 기록을 직접 읽고 `StateStore.record_state`로 지금 상태를 계산해, 자기가
맡은 상태일 때만 일한다. 여러 상태에 해당하면 아래 순서의 앞쪽이 우선한다. 기록은 `forecast_records`와 `allocations`이고, 인스턴스는
forecast 기록이면 그 인스턴스(`agent_id`), `allocations`이면 그 라인(`line_id`)이다.

- `awaiting_business`(업무 대기): 그 인스턴스에 처리되지 않은 `human_manager` 담당 `intervention` escalation 기록이 있다. 담당은 human_manager.
- `awaiting_system`(시스템 대기): 그 인스턴스에 처리되지 않은 `system_operator` 담당 escalation 기록이 있다. 담당은 system_operator.
- `sent_back`(되돌려짐): `send_back`이 있다. 담당은 그 기록의 작성agent.
- `forwarded`(전달됨): 판정이 `passed` 또는 검증 면제 통과이고 `forward_to`가 있다. 담당은 `forward_to`의 agent.
- `completed`(완료): 판정이 `passed` 또는 검증 면제 통과이고 작성agent의 카드에 `produces`가 없다. 신호를 보내지 않는다.
- `judged`(판정됨): 판정(`validation`)이 있거나 검증 면제 통과다. 담당은 그 기록의 작성agent.
- `awaiting_validation`(검증 대기): 값이 있고 판정이 비어 있으며 검증 면제 통과가 아니고 `validation_settings`상 검증 대상이다. 담당은
  `validation_settings`의 검증agent.
- `none`: 위에 해당하지 않는다. 신호 대상이 아니다.

검증 면제 통과(`exempt_passed`)는 `selection_basis`가 `human`(사람이 escalation을 처리해 넣은 값) 또는 `expiry`(시한이 지나 정해진
처리로 진행한 값)인 forecast 기록이다. 판정 칸(`validation`)에는 검증agent의 판정만 쓰므로 이 기록의 판정 칸은 비어 있다.
"""

from typing import Any, Literal

from .escalation_records import has_open_escalation_of
from .state import AgentCard, EscalationRecord, ValidationSetting
from .validation_settings import validator_of

RecordState = Literal[
    "none", "awaiting_validation", "judged", "completed", "forwarded", "sent_back", "awaiting_system", "awaiting_business"
]

HUMAN_CHANNEL = "human_manager"
SYSTEM_CHANNEL = "system_operator"

# 보류: 처리되지 않은 escalation 기록이 있는 상태. 시작되거나 끝날 때 다음 agent 후보에게도 알린다(access.StateStore.update_state)
HOLD_STATES: frozenset[RecordState] = frozenset(["awaiting_business", "awaiting_system"])

# 상태를 가진 기록 컬렉션. 기록의 작성agent는 기록의 `role_tag`다. 새 컬렉션은 여기에 한 줄을 더한다
STATE_COLLECTIONS: tuple[str, ...] = ("forecast_records", "allocations")

_EXEMPT_BASES = frozenset(["human", "expiry"])


def is_exempt_passed(record: Any) -> bool:
    """검증 면제 통과인가. `selection_basis`가 없는 기록(`allocations`)은 해당하지 않는다."""
    return getattr(record, "selection_basis", None) in _EXEMPT_BASES


def _has_value(collection: str, record: Any) -> bool:
    """값이 있는가. forecast 기록은 `scenario`가 있을 때이고, `allocations`는 결론을 쓴 주기(`cycle`)가 있을 때다."""
    return record.scenario is not None if collection == "forecast_records" else record.cycle is not None


def compute_record_state(
    collection: str,
    record: Any,
    escalations: list[EscalationRecord],
    cards: list[AgentCard],
    settings: list[ValidationSetting],
) -> RecordState:
    """기록의 지금 상태."""
    agent_id = getattr(record, "agent_id", None)
    line_id = getattr(record, "line_id", None)
    if has_open_escalation_of(escalations, agent_id=agent_id, line_id=line_id, target_role="human_manager"):
        return "awaiting_business"
    if has_open_escalation_of(escalations, agent_id=agent_id, line_id=line_id, target_role="system_operator"):
        return "awaiting_system"
    if getattr(record, "send_back", None) is not None:
        return "sent_back"
    author = record.role_tag
    exempt = is_exempt_passed(record)
    validation = record.validation
    passed = exempt or (validation is not None and validation.status == "passed")
    if passed and getattr(record, "forward_to", None) is not None:
        return "forwarded"
    if passed and _produces_nothing(cards, author):
        return "completed"
    if validation is not None or exempt:
        return "judged"
    if _has_value(collection, record) and validator_of(settings, author) is not None:
        return "awaiting_validation"
    return "none"


def _produces_nothing(cards: list[AgentCard], author: str) -> bool:
    """작성agent의 카드가 있고 `produces`가 없는가. 카드가 없는 것은 카드 설정 문제라 완료로 보지 않는다."""
    card = next((c for c in cards if c.role_tag == author), None)
    return card is not None and card.produces is None


def owner_channel(collection: str, state: RecordState, record: Any, settings: list[ValidationSetting]) -> str | None:
    """상태의 담당 agent의 채널. 채널 이름은 그 agent의 `role_tag`다(GRAPH_FLOW.md "신호 규칙"). 신호를 보내지 않는 상태는 None."""
    if state == "awaiting_business":
        return HUMAN_CHANNEL
    if state == "awaiting_system":
        return SYSTEM_CHANNEL
    if state == "forwarded":
        return record.forward_to
    if state == "awaiting_validation":
        return validator_of(settings, record.role_tag)
    if state in ("judged", "sent_back"):
        return record.role_tag  # 판정됨, 되돌려짐: 기록을 쓴 작성agent
    return None  # 완료와 상태 없음
