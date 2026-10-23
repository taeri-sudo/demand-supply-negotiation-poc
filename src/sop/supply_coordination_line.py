"""supply_coordination이 라인 하나의 조율을 State에 반영한다 (AGENT_NODE_LIST.md supply_coordination agent "기록 쓰기와 검증").

`run_line_coordination`은 고정된 입력(`LineInputs`)으로 배분 규칙(`supply_allocation_rules.allocate_line`)을 한 번 돌리고 결과를
`update_state` 한 번으로 쓴다.
- 배분한 경우: 라인의 `allocations` 기록을 새 결론으로 덮어쓰고(`validation`은 비움 — 기록이 "검증 대기"가 되어 `allocation_validation`에게
  신호가 간다) 로그 `allocation_decided`(덮어쓰기 전 값 포함)와 약정 차이 알림(`commitment_gap` notice, 로그 `commitment_gap_noticed`)을
  함께 쓴다.
- `pegged_exceed_capacity`·`commitments_exceed_capacity`: 배분하지 않는다. 그 라인에 intervention escalation 기록과 로그
  `allocation_escalated`를 쓰고 이전 결론을 유지한다(기록이 "업무 대기"가 되어 human_manager에게 신호가 간다). 다른 라인은 영향받지
  않는다. `commitments_exceed_capacity`는 `interaction_protocol`에서 모드를 `notice`로 바꾸면 알리고 진행한다.

`check_allocation_verdict`는 배분 결론이 `failed`일 때 supply_coordination의 처리다: 입력(capacity, 재고, 수요 등)이 조율 때와 달라졌으면
다시 조율하라고 알리고, 같으면 배분 로직 오류로 `allocation_invalid` escalation(`system_operator`)을 만든다. 다시 조율하는 것은 입력이 바뀐
경우뿐이라 반복 횟수는 세지 않는다. 그 `allocation_invalid`를 `system_operator`가 판정 뒤에 처리했으면 입력을 비교하지 않는다: `rerun`이면 그
라인을 다시 조율하고, `dismiss`이면 이번 주기에서 뺀 라인이라 아무것도 하지 않는다.

supply_coordination은 `remaining_capacity`를 쓰지 않는다(읽기만). 라인의 결론은 하나이고 덮어쓰므로 공급 가능량을 다시 계산해도 이전 결론이
공급을 깎지 않는다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .access import FieldWrite, LogWrite, StateStore
from .escalation_records import is_open_hold, new_escalation
from .interaction_protocol import find_protocol_entry
from .judgment import StructuredJudgment
from .logging_utils import log
from .state import Allocation, EscalationRecord, InteractionProtocol, ValidationResult
from .supply_allocation_rules import LineInputs, allocate_line, commitment_gaps, entries_of
from .supply_coordination_human_manager import ESCALATION_EDGE

ROLE_TAG = "supply_coordination"
CoordinationOutcome = Literal["allocated", "pegged_exceed_capacity", "commitments_exceed_capacity"]
VerdictAction = Literal["waiting", "completed", "recoordinate", "invalid", "dismissed"]
CoordinationKind = Literal["regular", "continuing"]  # 정기 조율 / 이어지는 조율


@dataclass(frozen=True)
class CoordinationResult:
    outcome: CoordinationOutcome
    allocation: Allocation | None  # 새로 쓴 결론(배분하지 않았으면 None)
    escalations: list[EscalationRecord]  # 이번 조율이 더한 escalation 기록(intervention과 notice)


def find_allocation_index(store: StateStore, role_tag: str, line_id: str) -> int:
    allocations = store.get_field(role_tag, "allocations")
    for index, allocation in enumerate(allocations):
        if allocation.line_id == line_id:
            return index
    raise ValueError(f"allocations에 라인 {line_id!r}의 기록이 없음(시스템을 처음 설정할 때 라인마다 빈 기록을 만든다)")


def line_has_open_hold(store: StateStore, role_tag: str, line_id: str) -> bool:
    return any(is_open_hold(r) and r.line_id == line_id for r in store.get_field(role_tag, "escalation_records"))


def exceed_mode(protocol: list[InteractionProtocol]):
    entry = find_protocol_entry(protocol, ESCALATION_EDGE, "commitments_exceed_capacity")
    return entry.escalation_mode if entry is not None and entry.escalation_mode is not None else "intervention"


def run_line_coordination(
    store: StateStore,
    role_tag: str,
    inputs: LineInputs,
    protocol: list[InteractionProtocol] | None = None,
    *,
    judgment: StructuredJudgment | None = None,
    kind: CoordinationKind = "regular",
    late_changes: Sequence[dict] = (),
    ignored_refs: Sequence[str] = (),
) -> CoordinationResult:
    """라인 하나를 조율하고 결과를 State에 쓴다. 이미 처리되지 않은 intervention이 있는 라인은 조율하지 않는다(ValueError).

    `protocol`은 `interaction_protocol`(없으면 `store`의 것)이다. 약정 차이 알림 기준과 하한 초과의 모드를 여기서 읽는다. `judgment`는 호출한 쪽이
    `inputs`로 이미 계산한 배분이다(없으면 정기 조율의 배분을 여기서 계산한다). `kind`와 `late_changes`(결론 직전의 확인에서 마지막 순서로
    얹은 변경)와 `ignored_refs`(사람이 넣은 하한·묶임 수량 중 조율 시점에 수요가 사라져 무시한 `demand_ref`)는 조율 로그에 남긴다.
    """
    entries_protocol = protocol if protocol is not None else store.get_field(role_tag, "interaction_protocol")
    index = find_allocation_index(store, role_tag, inputs.line_id)
    if line_has_open_hold(store, role_tag, inputs.line_id):
        raise ValueError(f"{inputs.line_id}는 처리되지 않은 escalation이 있어 조율할 수 없음")
    previous = store.get_field(role_tag, f"allocations[{index}]")
    escalations = store.get_field(role_tag, "escalation_records")
    mode = exceed_mode(entries_protocol)
    judgment = judgment if judgment is not None else allocate_line(inputs, mode)
    outcome: CoordinationOutcome = judgment.judgment["outcome"]
    base_seq = store.next_log_seq()
    context = {
        "line_id": inputs.line_id, "cycle": inputs.cycle, "kind": kind, "inputs": inputs.model_dump(mode="json"),
        "judgment": judgment.judgment, "late_changes": list(late_changes), "ignored_resolution_refs": list(ignored_refs),
    }

    if outcome != "allocated":  # 배분하지 않는다. 이전 결론을 유지하고 그 라인을 사람에게 올린다
        hold = new_escalation(
            role_tag, outcome, judgment.reasoning, line_id=inputs.line_id, log_seq=base_seq,
            mode=mode if outcome == "commitments_exceed_capacity" else None,
        )
        store.update_state(
            role_tag,
            fields=[FieldWrite("escalation_records", [*escalations, hold])],
            logs=[LogWrite("allocation_escalated", None, payload={**context, "reason": outcome, "reasoning": judgment.reasoning})],
        )
        log(role_tag, "run_line_coordination", line_id=inputs.line_id, outcome=outcome)
        return CoordinationResult(outcome, None, [hold])

    entries = entries_of(judgment)
    new_records: list[EscalationRecord] = []
    logs = [
        LogWrite(
            "allocation_decided", None,
            payload={**context, "reasoning": judgment.reasoning, "previous": previous.model_dump(mode="json")},
        )
    ]
    if judgment.judgment["exceeded_notice"]:  # 하한 초과를 알리고 진행하는 모드
        new_records.append(
            new_escalation(
                role_tag, "commitments_exceed_capacity", "미납분과 계약 하한의 합이 남은 공급을 넘어 알리고 진행함",
                line_id=inputs.line_id, log_seq=base_seq + len(logs), mode="notice",
            )
        )
        logs.append(LogWrite("allocation_escalated", None, payload={**context, "reason": "commitments_exceed_capacity", "mode": "notice"}))
    gap_entry = find_protocol_entry(entries_protocol, ESCALATION_EDGE, "commitment_gap")
    if gap_entry is not None and gap_entry.notice_threshold is not None:
        for gap in commitment_gaps(inputs, entries, gap_entry.notice_threshold):
            new_records.append(
                new_escalation(
                    role_tag, "commitment_gap",
                    f"{gap['company_id']}/{gap['item_id']}: 배분 {gap['allocated']:g}이 약정의 이번 주기 몫 {gap['share']:g}과 {gap['gap_ratio']:.0%} 차이",
                    line_id=inputs.line_id, log_seq=base_seq + len(logs),
                )
            )
            logs.append(LogWrite("commitment_gap_noticed", None, payload={**context, "gap": gap}))
    allocation = Allocation(line_id=inputs.line_id, cycle=inputs.cycle, entries=entries, log_seq=base_seq, validation=None)
    fields = [FieldWrite(f"allocations[{index}]", allocation)]
    if new_records:
        fields.append(FieldWrite("escalation_records", [*escalations, *new_records]))
    store.update_state(role_tag, fields=fields, logs=logs)
    log(role_tag, "run_line_coordination", line_id=inputs.line_id, outcome=outcome, entries=len(entries), notices=len(new_records))
    return CoordinationResult(outcome, allocation, new_records)


def _handled_invalid(records: list[EscalationRecord], line_id: str, verdict: ValidationResult) -> EscalationRecord | None:
    """이 판정 뒤에 처리된(`resolved`) 그 라인의 `allocation_invalid` 기록. 판정 시각 이후에 처리한 것만 이 판정의 처리다."""
    if verdict.ts is None:
        return None
    judged_at = datetime.fromisoformat(verdict.ts)
    closed = [
        r for r in records
        if r.reason == "allocation_invalid" and r.line_id == line_id and r.status == "resolved" and r.resolution is not None
        and datetime.fromisoformat(r.resolution.ts) > judged_at
    ]
    return max(closed, key=lambda r: r.resolution.ts, default=None)  # pyright: ignore[reportOptionalMemberAccess]


def check_allocation_verdict(
    store: StateStore, role_tag: str, line_id: str, current_inputs: LineInputs
) -> VerdictAction:
    """라인의 결론이 `failed`로 판정됐을 때의 처리(판정 신호를 받은 supply_coordination이 부른다).

    - `"recoordinate"`: 입력이 조율 때와 달라졌거나, 이 판정의 `allocation_invalid`를 `system_operator`가 `rerun`으로 처리했다(이때는 입력을 비교하지
      않는다). 호출한 쪽이 `current_inputs`로 다시 조율한다.
    - `"invalid"`: 입력이 같은데 불합격이다(배분 로직 오류). `allocation_invalid` escalation 기록(system_operator)을 만들었다.
    - `"dismissed"`: 이 판정의 `allocation_invalid`를 `system_operator`가 `dismiss`로 처리해 이번 주기에서 뺀 라인이다. 아무것도 하지 않는다.
    - `"completed"`: 판정이 `passed`라 완료된 결론이다. `"waiting"`: 이 처리의 대상 상태가 아니다("판정됨"의 `failed`가 아님).
    """
    index = find_allocation_index(store, role_tag, line_id)
    path = f"allocations[{index}]"
    record = store.get_field(role_tag, path)
    state = store.record_state(role_tag, path)
    if state == "completed":
        return "completed"
    verdict: ValidationResult | None = record.validation
    if state != "judged" or verdict is None or verdict.status != "failed":
        return "waiting"
    handled = _handled_invalid(store.get_field(role_tag, "escalation_records"), line_id, verdict)
    if handled is not None:  # 이 판정의 allocation_invalid를 system_operator가 이미 처리했다
        return "recoordinate" if handled.resolution is not None and handled.resolution.kind == "rerun" else "dismissed"
    decided = next((e for e in store.role_log(role_tag, role_tag) if e.seq == record.log_seq), None)
    if decided is None:
        raise ValueError(f"{path}의 log_seq({record.log_seq})가 가리키는 조율 로그 항목이 없음")
    if decided.payload["inputs"] != current_inputs.model_dump(mode="json"):
        return "recoordinate"
    seq = store.next_log_seq()
    hold = new_escalation(
        role_tag, "allocation_invalid", f"입력이 같은데 배분 결론이 검증에서 불합격: {verdict.rationale}", line_id=line_id, log_seq=seq
    )
    store.update_state(
        role_tag,
        fields=[FieldWrite("escalation_records", [*store.get_field(role_tag, "escalation_records"), hold])],
        logs=[LogWrite("allocation_escalated", None, payload={"line_id": line_id, "reason": "allocation_invalid", "verdict_ts": verdict.ts})],
    )
    return "invalid"
