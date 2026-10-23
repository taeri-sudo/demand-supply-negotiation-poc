"""forecast의 escalation 시한 초과 처리 (STATE_SCHEMA.md 7번 절 "시한 초과", AGENT_NODE_LIST.md "사람 escalation 세 경우").

시한이 되면 스케줄러가 기록을 만든 agent의 채널로 신호를 보내고, 그 agent는 escalation 기록이 아직 `open`이고 시한이 지났으면 reason 대응표
(`escalation_records.ESCALATION_REASONS`)의 처리를 한다. `status`를 `expired`로 쓰고 로그 `escalation_expired`를 남긴다.
- `proceed_with_value`(`selection_unresolved`, `options_exhausted`): 기록의 `scenario`(규칙이 낸 중간값, 재실행 전 값)를 그대로 두고
  `selection_basis`를 `"expiry"`로 쓴다. 사람의 처리와 같이 판정 칸·`forward_to`·`send_back`은 비워 검증 면제 통과가 되고 forecast가 다음 agent로
  넘긴다.
- `proceed_without_demand`(`no_computable_assumption`): 기록의 `scenario`와 `selection_basis`는 `null`로 남고 이번 주기 조율은 이 인스턴스를
  기다리지 않는다.
시한 초과 처리가 없는 reason(system_operator 담당 등)은 시한이 없어 이 처리가 아무것도 하지 않는다.
"""

from datetime import datetime, timezone
from typing import Literal

from .access import FieldWrite, LogWrite, StateStore
from .escalation_records import OPEN_STATUS, rule_of
from .logging_utils import log

ExpiryResult = Literal["expired_with_value", "expired_without_demand", "waiting"]
FORECAST_ROLE_TAG = "forecast"


def expire_forecast_escalation(
    store: StateStore, forecast_role_tag: str, index: int, now: datetime | None = None
) -> ExpiryResult:
    """`index`번 escalation 기록의 시한 초과를 처리한다. 처리 대상이 아니면(forecast가 만든 기록이 아님, 이미 처리됨, 시한 전, 시한 초과
    처리가 없는 reason) `"waiting"`이고 아무것도 쓰지 않는다."""
    current = now or datetime.now(timezone.utc)
    escalation = store.get_field(forecast_role_tag, "escalation_records")[index]
    if (
        not escalation.trigger_edge.startswith(f"{FORECAST_ROLE_TAG}->") or escalation.mode != "intervention"
        or escalation.status != OPEN_STATUS or escalation.deadline is None or escalation.agent_id is None
    ):
        return "waiting"
    action = rule_of(escalation.reason).on_expiry
    if action is None or datetime.fromisoformat(escalation.deadline) > current:
        return "waiting"
    expired = escalation.model_copy(update={"status": "expired"})
    fields = [FieldWrite(f"escalation_records[{index}]", expired)]
    if action == "proceed_with_value":
        path = next(
            f"forecast_records[{i}]" for i, r in enumerate(store.get_field(forecast_role_tag, "forecast_records"))
            if r.agent_id == escalation.agent_id
        )
        if store.get_field(forecast_role_tag, path).scenario is None:
            raise ValueError(f"{escalation.reason}는 값으로 진행하는 reason인데 {escalation.agent_id}의 scenario가 없음")
        fields += [
            FieldWrite(f"{path}.selection_basis", "expiry"),
            FieldWrite(f"{path}.validation", None),
            FieldWrite(f"{path}.forward_to", None),
            FieldWrite(f"{path}.send_back", None),
        ]
    store.update_state(
        forecast_role_tag,
        fields=fields,
        logs=[
            LogWrite(
                "escalation_expired", escalation.agent_id,
                payload={"escalation": expired.model_dump(mode="json"), "action": action, "expired_at": current.isoformat()},
            )
        ],
    )
    log(forecast_role_tag, "expire_forecast_escalation", agent_id=escalation.agent_id, reason=escalation.reason, action=action)
    return "expired_with_value" if action == "proceed_with_value" else "expired_without_demand"


def expire_due_forecast_escalations(store: StateStore, forecast_role_tag: str, now: datetime | None = None) -> list[ExpiryResult]:
    """시한이 지난 forecast의 escalation 기록을 모두 처리한다(스케줄러가 보낸 신호를 받았을 때). 처리한 결과 목록을 돌려준다."""
    results: list[ExpiryResult] = []
    for index in range(len(store.get_field(forecast_role_tag, "escalation_records"))):
        result = expire_forecast_escalation(store, forecast_role_tag, index, now)
        if result != "waiting":
            results.append(result)
    return results
