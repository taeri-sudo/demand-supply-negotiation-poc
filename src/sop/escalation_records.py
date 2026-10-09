"""`escalation_records` 공통 규칙: 미처리 intervention 판정과 reason→긴급도 대응표.

- 처리되지 않은(open) `intervention` escalation 기록이 있는 인스턴스는 검증agent로도 다음 agent로도 넘기지 않는다
  (GRAPH_FLOW.md "사람 입력과 검증의 무결성"). `has_open_intervention`이 이 판정이다.
- 긴급도는 State 필드가 아니라 이 모듈의 reason→긴급도 대응표로 정한다(STATE_SCHEMA.md 7번 절). `emergency`는 진행할
  값이 없거나 시스템 장애인 경우, `warning`은 값은 있지만 확인이 필요한 경우다. escalation 기록을 만드는 코드가 쓰는
  reason은 모두 이 표에 있어야 한다.
"""

from typing import Literal

from .state import EscalationRecord

OPEN_STATUS = "open"
Urgency = Literal["emergency", "warning"]

ESCALATION_URGENCY: dict[str, Urgency] = {
    "validation_error": "emergency",  # 시스템 장애: 검증 절차가 비정상 종료
    "forecast_run_error": "emergency",  # 시스템 장애: forecast 실행이 예외로 끝남
    "no_computable_assumption": "emergency",  # 진행할 값이 없음
    "options_exhausted": "emergency",  # 직전 값은 검증에서 문제가 있다고 돌려보낸 값이고 대응할 수단이 모두 소진됨
    "selection_unresolved": "warning",  # 규칙이 낸 중간값은 있지만 어느 가정을 믿을지 확인이 필요함
    "no_next_agent": "warning",  # 값은 있지만 넘길 agent가 없음
    "multiple_next_agents": "warning",  # 값은 있지만 넘길 agent를 고르지 못함
    "commitment_gap": "warning",  # 약정과 배분의 차이 알림(진행을 멈추지 않음)
}


def urgency_of(reason: str) -> Urgency:
    """reason의 긴급도. 표에 없는 reason은 오류다(새 reason을 만들 때 표에 한 줄을 더한다)."""
    try:
        return ESCALATION_URGENCY[reason]
    except KeyError:
        raise ValueError(f"긴급도 대응표에 없는 reason: {reason!r}") from None


def has_open_intervention(records: list[EscalationRecord], agent_id: str | None) -> bool:
    """인스턴스(`agent_id`)에 처리되지 않은(open) `intervention` escalation 기록이 있는지. 어떤 엣지의 기록이든 본다."""
    if agent_id is None:
        return False
    return any(r.agent_id == agent_id and r.mode == "intervention" and r.status == OPEN_STATUS for r in records)
