"""`escalation_records` 공통 규칙: reason 대응표(담당·mode·긴급도)와 미처리 escalation 판정.

- 담당(`target_role`)·mode·긴급도는 State 필드가 아니라 이 모듈의 reason 대응표로 정한다(STATE_SCHEMA.md 7번 절). `emergency`는
  진행할 값이 없거나 시스템 장애인 경우, `warning`은 값은 있지만 확인이 필요한 경우다. escalation 기록을 만드는 코드가 쓰는
  reason은 모두 이 표에 있어야 하고, 만들 때는 `new_escalation`으로 표의 값을 채운다.
- 처리되지 않은(open) escalation 기록이 있는 인스턴스는 검증agent로도 다음 agent로도 넘기지 않는다(GRAPH_FLOW.md "사람 입력과 검증의
  무결성"). 인스턴스는 forecast 기록이면 `agent_id`, `allocations`이면 `line_id`로 알아본다.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .coordination_schedule import next_daily_coordination
from .state import EscalationMode, EscalationRecord, EscalationTarget

OPEN_STATUS = "open"
Urgency = Literal["emergency", "warning"]
# 시한이 지났을 때의 처리: 기록의 값(`scenario`)을 그대로 두고 진행 / 예측 수요 없이 진행
ExpiryAction = Literal["proceed_with_value", "proceed_without_demand"]


@dataclass(frozen=True)
class ReasonRule:
    """reason 하나의 담당·mode·긴급도·시한 초과 처리. 시한 초과 처리가 없으면(None) 시한도 없고 처리될 때까지 대기한다."""

    target_role: EscalationTarget
    mode: EscalationMode
    urgency: Urgency
    on_expiry: ExpiryAction | None = None


_HUMAN = "human_manager"
_SYSTEM = "system_operator"

ESCALATION_REASONS: dict[str, ReasonRule] = {
    "no_computable_assumption": ReasonRule(_HUMAN, "intervention", "emergency", "proceed_without_demand"),  # 진행할 값이 없음
    "selection_unresolved": ReasonRule(_HUMAN, "intervention", "warning", "proceed_with_value"),  # 규칙이 낸 중간값은 있지만 어느 가정을 믿을지 확인이 필요함
    "options_exhausted": ReasonRule(_HUMAN, "intervention", "warning", "proceed_with_value"),  # 재실행 전에 계산된 값이 그대로 남아 있음
    "forecast_run_error": ReasonRule(_SYSTEM, "intervention", "emergency"),  # 시스템 장애: forecast 실행이 예외로 끝남
    "no_next_agent": ReasonRule(_SYSTEM, "intervention", "warning"),  # 값은 있지만 넘길 agent가 없음(카드 설정 문제)
    "multiple_next_agents": ReasonRule(_SYSTEM, "intervention", "warning"),  # 값은 있지만 넘길 agent를 고르지 못함(카드 설정 문제)
    "validation_error": ReasonRule(_SYSTEM, "intervention", "emergency"),  # 시스템 장애: 검증 절차가 비정상 종료
    "commitments_exceed_capacity": ReasonRule(_HUMAN, "intervention", "emergency"),  # mode는 interaction_protocol에서 바꿀 수 있다
    "pegged_exceed_capacity": ReasonRule(_HUMAN, "intervention", "emergency"),
    "allocation_invalid": ReasonRule(_SYSTEM, "intervention", "emergency"),  # 배분 로직 오류
    "commitment_gap": ReasonRule(_HUMAN, "notice", "warning"),  # 약정과 배분의 차이 알림(진행을 멈추지 않음)
}


def rule_of(reason: str) -> ReasonRule:
    """reason의 대응표 항목. 표에 없는 reason은 오류다(새 reason을 만들 때 표에 한 줄을 더한다)."""
    try:
        return ESCALATION_REASONS[reason]
    except KeyError:
        raise ValueError(f"reason 대응표에 없는 reason: {reason!r}") from None


def urgency_of(reason: str) -> Urgency:
    return rule_of(reason).urgency


def new_escalation(
    creator_role_tag: str,
    reason: str,
    rationale: str,
    *,
    agent_id: str | None = None,
    line_id: str | None = None,
    log_seq: int | None = None,
    deadline: str | None = None,
    mode: EscalationMode | None = None,
    now: datetime | None = None,
) -> EscalationRecord:
    """reason 대응표의 담당·mode로 escalation 기록 하나를 만든다. State에 쓰는 일은 호출부가 한다.

    `trigger_edge`는 `{만드는 agent의 role_tag}->{담당}`이다. `mode`를 주면 대응표의 mode 대신 쓴다(`interaction_protocol`에서
    바꿀 수 있는 reason용). 시한 초과 처리가 있는 reason의 intervention은 `deadline`을 정한다 — 주지 않으면 `now` 뒤 첫 정해진 조율
    시각이다.
    """
    rule = rule_of(reason)
    if deadline is None and rule.on_expiry is not None and (mode or rule.mode) == "intervention":
        deadline = next_daily_coordination(now or datetime.now(timezone.utc)).isoformat()
    return EscalationRecord(
        agent_id=agent_id,
        line_id=line_id,
        trigger_edge=f"{creator_role_tag}->{rule.target_role}",
        reason=reason,
        rationale=rationale,
        target_role=rule.target_role,
        mode=mode or rule.mode,
        log_seq=log_seq,
        deadline=deadline,
        status=OPEN_STATUS if (mode or rule.mode) == "intervention" else None,  # notice는 status가 없다
    )


def is_open_hold(record: EscalationRecord) -> bool:
    """처리되지 않은 `intervention` escalation 기록인가. `notice`는 진행을 멈추지 않는다."""
    return record.mode == "intervention" and record.status == OPEN_STATUS


def has_open_intervention(records: list[EscalationRecord], agent_id: str | None) -> bool:
    """인스턴스(`agent_id`)에 처리되지 않은(open) `intervention` escalation 기록이 있는지. 담당과 어떤 엣지의 기록이든 본다."""
    if agent_id is None:
        return False
    return any(r.agent_id == agent_id and is_open_hold(r) for r in records)


def has_open_escalation_of(
    records: list[EscalationRecord], *, agent_id: str | None, line_id: str | None, target_role: EscalationTarget
) -> bool:
    """인스턴스(forecast 기록이면 `agent_id`, 라인이면 `line_id`)에 그 담당의 처리되지 않은 `intervention` escalation 기록이 있는지."""
    return any(
        r.target_role == target_role and is_open_hold(r)
        and ((agent_id is not None and r.agent_id == agent_id) or (line_id is not None and r.line_id == line_id))
        for r in records
    )
