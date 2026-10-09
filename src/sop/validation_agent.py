"""검증agent 공통 틀: 신호를 받아 판정만 하고 라우팅은 하지 않는다 (GRAPH_FLOW.md "검증agent", "신호 규칙").

기록이 "검증 대기" 상태가 됐다는 신호(레코드 경로 `field_path`, 쓴 작성agent의 role_tag `written_by`)를 받으면 기록을 직접 읽고,
지금 상태가 "검증 대기"일 때만 판정한다. 이미 판정됐거나 "사람 대기"인 기록의 신호(늦게 도착한 신호 포함)는 넘어간다.
검증agent는 무기억 워커풀이라 매번 State를 새로 읽고, State와 이번 주기 스냅샷만 읽는다.

판정과 로그 항목을 한 번의 `update_state`로 쓰면 기록이 "판정됨"이 되어 작성agent의 채널(role_tag)로 신호가
자동으로 간다. 다음 agent에게는 전달하지 않는다. `passed`면 작성agent가 다음 agent에게 직접 보내고, `failed`면 작성agent가
처리한다. edge를 하드코딩하지 않는다(MILESTONES.md 공통 규칙 1). 이 모듈이 아는 것은 두 가지뿐이고 모두 설정이다.
- `interaction_protocol`에 작성agent의 edge 항목이 하나라도 있어야 신호를 처리한다. 없으면 오류다.
- 작성agent마다 검증 규칙(`ValidationRules`)이 있다. 규칙은 검증 대상 agent를 만드는 마일스톤에서 더한다.

`error`: 규칙 실행이 예외로 끝나면 재시도 없이 바로 `validation_error` escalation 기록을 판정, 로그 항목과 같은 갱신에 쓴다. 기록이
"사람 대기"가 되어 human_manager에게 신호가 가고, 그 인스턴스만 멈춘다. State를 쓰는 중의 예외는 삼키지 않고 올라간다.

`repeat_escalation_threshold`는 검증agent가 아니라 `failed`를 받은 작성agent가 쓴다.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .access import FieldWrite, LogWrite, StateStore
from .escalation_records import OPEN_STATUS
from .ids import now_iso
from .interaction_protocol import find_edges_of
from .logging_utils import log
from .record_state import record_state
from .state import (
    EscalationRecord,
    LogEvent,
    SuspectedCause,
    ValidationResult,
    suspected_cause_reason,
)

VALIDATION_ERROR_REASON = "validation_error"


@dataclass(frozen=True)
class ValidationRules:
    """작성agent 하나의 레코드를 검증하는 규칙.

    `check`는 대상 agent의 계산을 재현하지 않고 독립적인 제약조건만 확인해 위반을 의심되는 원인으로 돌려준다.
    `instance_id`는 레코드가 속한 인스턴스(`escalation_records.agent_id`와 같은 값)이고 인스턴스 단위가 아니면 None이다.
    """

    instance_id: Callable[[Any], str | None]
    check: Callable[[Any], list[SuspectedCause]]


def _run_check(
    check: Callable[[Any], list[SuspectedCause]], record: Any
) -> tuple[list[SuspectedCause] | None, str | None]:
    """규칙을 한 번 실행한다. 예외로 끝나면 재시도하지 않고 (None, 오류 내용)을 돌려준다."""
    try:
        return check(record), None
    except Exception as error:  # 검증 절차 자체의 비정상 종료는 종류와 상관없이 error다
        return None, f"{type(error).__name__}: {error}"


_VERDICT_EVENTS: dict[str, LogEvent] = {
    "passed": "validation_passed",
    "failed": "validation_failed",
    "error": "validation_error",
}


def _verdict_log(instance_id: str | None, result: ValidationResult) -> LogWrite:
    """판정의 로그 항목. payload에 판정 시각, 근거, 이유를 담는다."""
    return LogWrite(
        _VERDICT_EVENTS[result.status],
        instance_id,
        payload={
            "validation_ts": result.ts,
            "rationale": result.rationale,
            "suspected_causes": [cause.model_dump(mode="json") for cause in result.suspected_causes],
        },
    )


def validate_job(
    store: StateStore,
    validator_role_tag: str,
    message: Mapping[str, Any],
    rules_by_writer: Mapping[str, ValidationRules],
) -> ValidationResult | None:
    """신호 하나를 받아 기록이 "검증 대기"이면 판정해 `validation`에 쓴다. 판정하지 않고 넘어가면 None을 반환한다."""
    record_path: str = message["field_path"]
    writer: str = message["written_by"]

    protocol = store.get_field(validator_role_tag, "interaction_protocol")
    if not find_edges_of(protocol, writer):
        raise ValueError(f"interaction_protocol에 {writer}의 edge 항목이 없음")
    if writer not in rules_by_writer:
        raise ValueError(f"{writer!r}의 검증 규칙이 없음")
    rules = rules_by_writer[writer]

    record = store.get_field(validator_role_tag, record_path)
    instance_id = rules.instance_id(record)
    state = record_state(record, store.get_field(validator_role_tag, "escalation_records"))
    if state != "awaiting_validation":
        log(validator_role_tag, "validate_job", record_path=record_path, skipped=state)
        return None

    causes, error = _run_check(rules.check, record)
    if causes is None:
        result = ValidationResult(
            status="error", rationale=f"검증 절차가 비정상 종료: {error}", ts=now_iso(), validator_role_tag=validator_role_tag
        )
    elif causes:
        result = ValidationResult(
            status="failed",
            suspected_causes=causes,
            rationale="; ".join(dict.fromkeys(_describe(c) for c in causes)),
            ts=now_iso(),
            validator_role_tag=validator_role_tag,
        )
    else:
        result = ValidationResult(
            status="passed", rationale="모든 검증 규칙 통과", ts=now_iso(), validator_role_tag=validator_role_tag
        )
    verdict_path = f"{record_path}.validation"
    fields = [FieldWrite(verdict_path, result)]
    if result.status == "error":  # 판정, 로그 항목, escalation 기록을 한 번의 State 갱신으로 쓴다(기록이 사람 대기가 된다)
        escalation = EscalationRecord(
            agent_id=instance_id,
            trigger_edge=f"{validator_role_tag}->human_manager",
            reason=VALIDATION_ERROR_REASON,
            rationale=result.rationale or "",
            mode="intervention",
            log_seq=store.next_log_seq(),  # 아래 로그 항목의 seq
            status=OPEN_STATUS,
        )
        fields.append(FieldWrite("escalation_records", [*store.get_field(validator_role_tag, "escalation_records"), escalation]))
    store.update_state(validator_role_tag, fields=fields, logs=[_verdict_log(instance_id, result)])
    log(validator_role_tag, "validate_job", record_path=record_path, status=result.status)
    return result


def _describe(cause: SuspectedCause) -> str:
    target = cause.assumption_id or (f"{cause.source.kind}/{cause.source.item_scope}" if cause.source else "")
    drivers = f" drivers={cause.drivers}" if cause.drivers is not None else ""
    return f"{suspected_cause_reason(cause)} {target}{drivers}".strip()


def process_pending_validations(
    store: StateStore,
    validator_role_tag: str,
    rules_by_writer: Mapping[str, ValidationRules],
) -> list[ValidationResult]:
    """검증agent 큐에 쌓인 신호를 모두 처리한다. 큐가 비면 끝난다."""
    queue = store.queue(validator_role_tag)
    results: list[ValidationResult] = []
    while not queue.empty():
        message = queue.get_nowait()
        result = validate_job(store, validator_role_tag, message, rules_by_writer)
        if result is not None:
            results.append(result)
    return results


async def run_validation_worker(
    store: StateStore,
    validator_role_tag: str,
    rules_by_writer: Mapping[str, ValidationRules],
) -> None:
    """워커풀의 워커 하나: 신호가 올 때마다 하나씩 pull해 처리한다. 같은 큐를 읽는 워커가 여럿이어도 된다."""
    queue = store.queue(validator_role_tag)
    while True:
        message = await queue.get()
        validate_job(store, validator_role_tag, message, rules_by_writer)
