"""기록의 상태와 신호 규칙 (GRAPH_FLOW.md "신호 규칙").

기록의 상태는 이미 있는 값에서 계산하며 따로 저장하지 않는다. State 갱신으로 기록의 상태가 바뀌면 `access.StateStore.update_state`가 그
상태의 담당에게 자동으로 신호를 보낸다. 받는 쪽은 신호를 받으면 기록을 직접 읽고 `record_state`로 지금 상태를 계산해, 자기가
맡은 상태일 때만 일한다. 여러 상태에 해당하면 아래 순서의 앞쪽이 우선한다.

- `awaiting_human`(사람 대기): 그 인스턴스에 처리되지 않은 `intervention` escalation 기록이 있다. 담당은 human_manager.
- `sent_back`(되돌려짐): `send_back`이 있다. 담당은 그 기록의 작성agent.
- `forwarded`(전달됨): 판정이 `passed`이고 `forward_to`가 있다. 담당은 `forward_to`의 agent.
- `judged`(판정됨): 판정(`validation`)이 있다. 담당은 그 기록의 작성agent.
- `awaiting_validation`(검증 대기): 값(`scenario`)이 있고 판정이 비어 있다. 담당은 검증agent.
- `none`: 위에 해당하지 않고 값도 없다. 신호 대상이 아니다.
"""

from typing import Any, Literal

from .escalation_records import has_open_intervention
from .state import EscalationRecord

RecordState = Literal["none", "awaiting_validation", "judged", "forwarded", "sent_back", "awaiting_human"]

FORECAST_VALIDATOR_ROLE_TAG = "forecast_validation"
HUMAN_CHANNEL = "human_manager"

# 상태를 가진 기록 컬렉션 → 그 기록의 검증agent role_tag. 새 컬렉션은 여기에 한 줄을 더한다
VALIDATOR_OF_COLLECTION: dict[str, str] = {"forecast_records": FORECAST_VALIDATOR_ROLE_TAG}


def record_state(record: Any, escalations: list[EscalationRecord]) -> RecordState:
    """기록의 지금 상태. 기록은 `agent_id`, `scenario`, `validation`, `forward_to`, `send_back` 필드를 가진다."""
    if has_open_intervention(escalations, record.agent_id):
        return "awaiting_human"
    if record.send_back is not None:
        return "sent_back"
    if record.validation is not None and record.validation.status == "passed" and record.forward_to is not None:
        return "forwarded"
    if record.validation is not None:
        return "judged"
    if record.scenario is not None:
        return "awaiting_validation"
    return "none"


def owner_channel(state: RecordState, record: Any, validator_role_tag: str) -> str:
    """상태의 담당 agent의 채널. 채널 이름은 그 agent의 `role_tag`다(GRAPH_FLOW.md "신호 규칙")."""
    if state == "awaiting_human":
        return HUMAN_CHANNEL
    if state == "forwarded":
        return record.forward_to
    if state == "awaiting_validation":
        return validator_role_tag
    return record.role_tag  # 판정됨, 되돌려짐: 기록을 쓴 작성agent
