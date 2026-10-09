"""forecast agent → 검증agent → supply_coordination agent 단방향 최적화 배분 (M1, M3에서 검증 경유로 변경).

GRAPH_FLOW.md "상호작용 세 가지 유형"의 "최적화"에 해당하는 정방향 edge —
forecast가 정한 최종 요청량(시나리오)을 검증agent(`forecast_validation`)가 판정하고, `passed`면 forecast가
supply_coordination에게 직접 보내 supply_coordination이 그 값을 그대로 담은 `allocation_candidate` 하나를
생성한다. 라운드도, capacity 비교도, 응답
대기도 없다(우선순위 점수 산출은 (회사,item) 인스턴스가 1개뿐이라 경쟁
자체가 없어 이 마일스톤에서는 값을 그대로 반영하는 것으로 근사 — 실제
다인스턴스 경쟁 로직은 M4).

흐름은 세 단계이고 서로 State와 신호(`asyncio.Queue`)로만 이어진다. 신호는 기록의 상태가 바뀔 때 자동으로 가고(`record_state.py`),
받는 쪽은 기록을 읽어 자기 상태일 때만 일한다.
1. forecast: 가정 선택까지 끝난 뒤 기록과 그 로그 항목을 한 번의 `update_state`로 쓴다. 보류가 아니면 기록이 "검증 대기"가 되어
   검증agent에게 신호가 가고, 보류이면 "사람 대기"가 되어 human_manager에게 신호가 간다.
2. 검증agent(`validation_agent.py`): 판정만 기록한다. 기록이 "판정됨"이 되면 forecast에게 신호가 간다. forecast는
   `check_forecast_verdict`로 기록을 읽어 자기 담당 상태일 때만 일한다: "판정됨"에서 `passed`면 `forward_validated_forecast`로
   카드(`agent_cards`)로 고른 다음 agent를 `forward_to`에 써서 넘기고(`select_next_agent`, 기록이 "전달됨"이 되어 그 agent에게 신호가 간다.
   후보가 없거나 여럿이면 escalation), `failed`면 `suspected_causes` 전체를 한 번의 재실행으로 처리해 다시 쓴다. "되돌려짐"(`send_back`)이면
   `misrouted`는 재실행 없이 다음 agent 선택만 다시 하고, 그 밖의 이유는 같은 재실행을 한다.
3. supply_coordination: `allocate_validated_forecast`가 기록을 읽어 "전달됨"이고 `forward_to`가 자기일 때 `allocation_candidate`를
   만든다. 자기 카드의 `accepts`와 맞지 않으면 `send_back_misrouted`로 되돌린다.

값 문제로 `send_back`을 쓰는 쪽(supply_coordination, plan agent의 infeasible이 트리거)은 plan agent 자체가 아직 없어(M5) 이 모듈에서
다루지 않는다. 받는 쪽(forecast)의 처리는 `check_forecast_verdict`에 있다.

`run_forecast_steps_select_and_submit`은 내부 단계 1-4(`forecast_steps.py`)부터 시작하는 첫 실행이고,
`rerun_forecast_steps_select_and_submit`은 재실행한 결과를 같은 경로로 이어 쓴다.
forecast_records 인스턴스는 리스트 순서가 아니라 (company_id, item_id)로
식별한다(STATE_SCHEMA.md "forecast_records[]" — agent_id는 표시용 합성키일
뿐 조회 키가 아니다) — `_find_forecast_record_index`가 그 조회를 담당한다.

실행의 계산 부분(내부 단계와 가정 선택)만 예외를 잡아 `forecast_run_error`로 처리한다. State를 쓰는 부분(`update_state`)의 예외는
삼키지 않고 그대로 올라간다.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from .access import FieldWrite, LogWrite, StateStore
from .agent_cards import accepts_record, candidate_agents
from .external_data import InstanceInputs
from .escalation_records import OPEN_STATUS
from .forecast_assumption_selection import select_forecast_assumption
from .forecast_human_manager import (
    ForecastEscalationReason,
    has_open_escalation,
    new_forecast_escalation,
    no_computable_rationale,
    options_exhausted_rationale,
)
from .forecast_steps import (
    ForecastRerunHandling,
    ForecastStepsResult,
    StageTracker,
    apply_steps_to_record,
    run_forecast_steps,
)
from .ids import new_id, now_iso
from .logging_utils import log
from .record_state import record_state
from .state import (
    AllocationCandidate,
    Assumption,
    ForecastRecord,
    InteractionProtocol,
    LogEvent,
    Scenario,
    SendBack,
    SuspectedCause,
    suspected_cause_reason,
)

FORECAST_SUPPLY_EDGE = "forecast<->supply_coordination"
T = TypeVar("T")
# forecast가 새로 쓰는 값은 아직 검증받지 않았으므로 판정, 넘김, 되돌림을 비운다
_UNVALIDATED: dict[str, None] = {"validation": None, "forward_to": None, "send_back": None}
RerunTrigger = Literal["failed_verdict", "send_back"]  # 재실행의 계기: 불합격 판정 또는 send-back
NextAgentResult = Literal["forwarded", "next_agent_unresolved", "waiting"]


def forecast_supply_protocol_entry() -> InteractionProtocol:
    """`forecast<->supply_coordination` 항목(STATE_SCHEMA.md 5번 절). 정방향·역방향 모두 라운드가 없어 `max_rounds`가 없다."""
    return InteractionProtocol(
        edge=FORECAST_SUPPLY_EDGE,
        repeat_escalation_threshold=2,
        scope=["forecast", "supply_coordination"],
        source="initial_design",
        last_updated=now_iso(),
    )


def _find_forecast_record_index(
    store: StateStore, role_tag: str, company_id: str, item_id: str
) -> int:
    """(company_id, item_id)에 일치하는 forecast_records 인스턴스의 리스트 인덱스를 찾는다.

    한 회사가 여러 item을 동시에 주문할 수 있어(예: 라면과 과자) 인스턴스
    단위는 회사 하나가 아니라 (company_id, item_id) 조합이다 — 조회도 이
    조합으로 해야, 항목 순서가 바뀌거나 다른 인스턴스가 먼저/나중에
    처리돼도 엉뚱한 인스턴스를 건드리지 않는다.
    """
    records = store.get_field(role_tag, "forecast_records")
    for index, record in enumerate(records):
        if record.company_id == company_id and record.item_id == item_id:
            return index
    raise ValueError(
        f"forecast_records에 company_id={company_id!r}, item_id={item_id!r} 인스턴스가 없음"
    )


def allocate_forecast_candidate(forecast_agent_id: str, value: float) -> AllocationCandidate:
    """forecast가 정한 최종 요청량(시나리오)을 그대로 담아 allocation_candidate 1개를 생성.

    우선순위 점수 산출은 회사가 1개뿐이라 경쟁 자체가 없어(M4에서 실제
    다회사 경쟁 로직 구현), 이번 마일스톤에서는 supply_coordination의
    agent 판단이 아니라 순수 함수로 근사한다.
    """
    return AllocationCandidate(
        plan_id=new_id("PLAN"),
        allocation={forecast_agent_id: value},
    )


def _skip_if_held(store: StateStore, forecast_role_tag: str, agent_id: str, run_kind: str) -> bool:
    """처리되지 않은 `intervention` escalation 기록이 있는 인스턴스는 첫 실행을 포함해 실행하지 않고, 건너뛴 사실만 forecast 로그에
    남긴다(`forecast_run_skipped`). 건너뛰었으면 True를 반환한다."""
    open_reasons = [
        r.reason for r in store.get_field(forecast_role_tag, "escalation_records")
        if r.agent_id == agent_id and r.mode == "intervention" and r.status == OPEN_STATUS
    ]
    if not open_reasons:
        return False
    store.append_log(
        forecast_role_tag, "forecast_run_skipped", agent_id=agent_id,
        payload={"run": run_kind, "open_reasons": open_reasons},
    )
    return True


def _hold(
    store: StateStore,
    forecast_role_tag: str,
    agent_id: str,
    reason: ForecastEscalationReason,
    rationale: str,
    log_event: LogEvent,
    log_payload: dict,
    record_path: str | None = None,
    record: ForecastRecord | None = None,
    *,
    extra_logs: Sequence[LogWrite] = (),
) -> None:
    """보류에 들어간다: escalation 기록, 로그 항목, forecast 기록(`record_path`를 주면)을 한 번의 State 갱신(`write_held`)으로
    쓴다. escalation 기록의 `log_seq`는 쓸 로그 항목의 seq를 먼저 정해 넣는다. `extra_logs`는 그 앞에 함께 쓰는 로그 항목이다."""
    escalation = new_forecast_escalation(agent_id, reason, rationale, store.next_log_seq() + len(extra_logs))
    store.write_held(
        forecast_role_tag, agent_id=agent_id, escalation=escalation, log_event=log_event, log_payload=log_payload,
        extra_logs=extra_logs, field_path=record_path, value=record,
    )


@dataclass(frozen=True)
class _Plan:
    """가정 선택까지 끝난 결과: 무엇을 어떻게 쓸지. 계산만 하고 State는 쓰지 않는다."""

    agent_id: str
    record_path: str
    record: ForecastRecord
    log_event: LogEvent
    log_payload: dict
    hold_reason: ForecastEscalationReason | None = None  # None이면 보류가 아니다
    hold_rationale: str = ""


def _plan_selection(
    store: StateStore,
    forecast_role_tag: str,
    company_id: str,
    item_id: str,
    assumptions: list[Assumption],
    no_assumption_rationale: str | None,
    after_rerun: bool,
    base_record: ForecastRecord | None,
) -> _Plan:
    """가정들에서 최종 요청량(시나리오)을 정하고 쓸 내용을 계획한다. 보류가 되는 경우(AGENT_NODE_LIST.md "사람 escalation 세 경우"):
    - 계산된 가정이 하나도 없으면(`assumptions`가 비어 있음) `scenario`와 `selection_basis`를 `null`로 두고
      `no_computable_assumption`이다. 재실행한 결과(`after_rerun`)이고 재실행 전에 계산된 `scenario`가
      있었으면 그 값을 그대로 두고 `options_exhausted`다.
    - 가정 선택 ③이면 규칙이 낸 중간값을 `scenario`에 두고(`selection_basis`는 `"rule"`) `selection_unresolved`다.
    """
    record_index = _find_forecast_record_index(store, forecast_role_tag, company_id, item_id)
    record_path = f"forecast_records[{record_index}]"
    stored = store.get_field(forecast_role_tag, record_path)
    record = base_record if base_record is not None else stored

    if not assumptions and after_rerun and stored.scenario is not None:
        rationale = no_assumption_rationale or "재실행으로 가정이 모두 제외됨"
        payload = {  # escalation 시점의 값: 유지한 scenario와 가정 목록, 제외된 가정
            "rationale": rationale,
            "scenario": stored.scenario.model_dump(mode="json"),
            "assumptions": [a.model_dump(mode="json") for a in stored.assumptions],
            "excluded_assumptions": [e.model_dump(mode="json") for e in record.excluded_assumptions],
        }
        return _Plan(record.agent_id, record_path, record, "scenario_options_exhausted", payload, "options_exhausted", rationale)

    if not assumptions:
        rationale = no_assumption_rationale or "계산된 가정이 하나도 없어 요청량을 만들 수 없음"
        payload = {  # escalation 시점의 값: 제외된 가정과 이유
            "rationale": rationale,
            "excluded_assumptions": [e.model_dump(mode="json") for e in record.excluded_assumptions],
        }
        emptied = record.model_copy(
            update={"assumptions": [], "scenario": None, "selection_basis": None, **_UNVALIDATED}
        )
        return _Plan(record.agent_id, record_path, emptied, "scenario_not_computable", payload, "no_computable_assumption", rationale)

    selection = select_forecast_assumption(assumptions)
    scenario = Scenario(**selection.judgment["scenario"])
    held = bool(selection.judgment["escalate"])
    updated = record.model_copy(
        update={"assumptions": assumptions, "scenario": scenario, "selection_basis": "rule", **_UNVALIDATED}
    )
    payload = {  # 이 시점의 값은 로그에 남는다(보류이면 escalation 기록이 이 항목을 가리킨다)
        "derivation": scenario.derivation, "value": scenario.value, "assumption_ids": scenario.assumption_ids,
        "held": held, "selection_basis": "rule", "assumption_values": {a.assumption_id: a.value for a in assumptions},
    }
    return _Plan(
        record.agent_id, record_path, updated, "scenario_decided", payload,
        "selection_unresolved" if held else None, selection.reasoning if held else "",
    )


def _apply_plan(store: StateStore, forecast_role_tag: str, plan: _Plan, extra_logs: Sequence[LogWrite] = ()) -> bool:
    """계획을 State에 쓴다. 쓰는 도중 예외가 나면 State는 이전 그대로이고 예외는 그대로 올라간다. 보류가 아니면 True를 반환한다.

    보류가 아니면 기록과 로그 항목을 한 번의 `update_state`로 쓴다(기록이 "검증 대기"가 되어 검증agent에게 신호가 간다).
    보류이면 escalation 기록과 함께 `write_held`로 쓴다(기록이 "사람 대기"가 되어 human_manager에게 신호가 간다).
    """
    if plan.hold_reason is not None:
        _hold(
            store, forecast_role_tag, plan.agent_id, plan.hold_reason, plan.hold_rationale, plan.log_event,
            plan.log_payload, plan.record_path, plan.record, extra_logs=extra_logs,
        )
        return False
    store.update_state(
        forecast_role_tag,
        fields=[FieldWrite(plan.record_path, plan.record)],
        logs=[*extra_logs, LogWrite(plan.log_event, plan.agent_id, payload=plan.log_payload)],
    )
    return True


async def run_forecast_select_and_submit(
    store: StateStore,
    forecast_role_tag: str,
    company_id: str,
    item_id: str,
    assumptions: list[Assumption],
    no_assumption_rationale: str | None = None,
    after_rerun: bool = False,
    base_record: ForecastRecord | None = None,
    extra_logs: Sequence[LogWrite] = (),
) -> bool:
    """가정 선택과 기록 쓰기. 보류가 아니라서 기록이 "검증 대기"가 되면 True를 반환한다. 인스턴스는 (company_id, item_id)로 지정한다.

    forecast는 한 번 실행하는 동안 `forecast_records`에 중간 결과를 쓰지 않고, 가정 선택까지 끝난 뒤 기록을 한 번만 쓴다.
    `base_record`는 내부 단계 1-4의 결과를 반영한 기록이고(없으면 State의 현재 기록), `extra_logs`는 같은 갱신에 먼저 쓰는 로그
    항목이다(예: 재실행 항목). 처리되지 않은 `intervention` escalation 기록이 있는 인스턴스는 실행하지 않고 건너뛴 사실만 로그에 남긴다.
    """
    record_index = _find_forecast_record_index(store, forecast_role_tag, company_id, item_id)
    agent_id = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]").agent_id
    if _skip_if_held(store, forecast_role_tag, agent_id, "select"):
        return False
    plan = _plan_selection(
        store, forecast_role_tag, company_id, item_id, assumptions, no_assumption_rationale, after_rerun, base_record
    )
    return _apply_plan(store, forecast_role_tag, plan, extra_logs)


def _misrouted_returners(store: StateStore, forecast_role_tag: str, record: ForecastRecord) -> set[str]:
    """같은 기록(같은 판정)을 `misrouted`로 되돌린 agent. 반송한 agent의 로그(`misrouted_send_back`)에서 찾는다."""
    validation_ts = record.validation.ts if record.validation is not None else None
    return {
        entry.payload["from_role"]
        for entry in store.negotiation_log(forecast_role_tag)
        if entry.event == "misrouted_send_back" and entry.agent_id == record.agent_id
        and entry.payload.get("validation_ts") == validation_ts
    }


def select_next_agent(store: StateStore, forecast_role_tag: str, company_id: str, item_id: str) -> NextAgentResult:
    """작성agent가 기록을 읽어 다음 agent를 골라 `forward_to`에 쓴다(GRAPH_FLOW.md "다음 agent 선택").

    - "판정됨"이고 `passed`인 기록, 또는 `misrouted`로 되돌려진 기록(`send_back`을 비우고 다시 고른다)만 대상이다. 그 밖에는 `"waiting"`.
    - 후보(카드의 `known_agents` 중 `accepts`가 맞는 agent, 이 기록을 `misrouted`로 되돌린 agent 제외)가 하나면 `forward_to`에 쓰고
      `"forwarded"`, 후보가 없으면 `no_next_agent`, 여럿이면 `multiple_next_agents` escalation을 만들고 `"next_agent_unresolved"`를 반환한다.
    - 같은 판정을 두 번 넘기지 않는 것은 상태로 판단한다("전달됨"인 기록은 대상이 아니다).
    """
    record_index = _find_forecast_record_index(store, forecast_role_tag, company_id, item_id)
    record_path = f"forecast_records[{record_index}]"
    record = store.get_field(forecast_role_tag, record_path)
    state = record_state(record, store.get_field(forecast_role_tag, "escalation_records"))
    validation = record.validation
    if validation is None or validation.status != "passed" or record.scenario is None:
        return "waiting"
    if state == "sent_back" and record.send_back is not None and record.send_back.is_misrouted:
        base = record.model_copy(update={"send_back": None, "forward_to": None})
    elif state == "judged":
        base = record
    else:
        return "waiting"
    excluded = _misrouted_returners(store, forecast_role_tag, record)
    candidates = candidate_agents(store.get_field(forecast_role_tag, "agent_cards"), record.role_tag, excluded)
    if len(candidates) == 1:
        store.update_state(  # 기록과 로그 항목을 한 번의 State 갱신으로 쓴다
            forecast_role_tag,
            fields=[FieldWrite(record_path, base.model_copy(update={"forward_to": candidates[0]}))],
            logs=[LogWrite("forwarded", record.agent_id, payload={"validation_ts": validation.ts, "forward_to": candidates[0]})],
        )
        return "forwarded"
    reason: ForecastEscalationReason = "no_next_agent" if not candidates else "multiple_next_agents"
    rationale = (
        "넘길 수 있는 agent가 없음" if not candidates else f"넘길 수 있는 agent가 여럿이라 고르지 못함: {', '.join(candidates)}"
    ) + (f" (되돌린 agent 제외: {', '.join(sorted(excluded))})" if excluded else "")
    _hold(
        store, forecast_role_tag, record.agent_id, reason, rationale, "next_agent_unresolved",
        {"reason": reason, "candidates": candidates, "excluded": sorted(excluded), "validation_ts": validation.ts},
        record_path, base,
    )
    return "next_agent_unresolved"


def forward_validated_forecast(store: StateStore, forecast_role_tag: str, company_id: str, item_id: str) -> bool:
    """`select_next_agent`로 다음 agent에게 넘겼으면 True를 반환한다."""
    return select_next_agent(store, forecast_role_tag, company_id, item_id) == "forwarded"


def send_back_misrouted(store: StateStore, receiver_role_tag: str, record_path: str) -> None:
    """"전달됨" 기록을 받은 agent가 자기 일이 아니라고 판단해 `misrouted`로 되돌린다. `send_back`과 로그 항목(`misrouted_send_back`)을
    한 번의 State 갱신으로 쓴다. 로그의 판정 시각으로 작성agent가 같은 기록에 대한 반송인지 알아본다."""
    record = store.get_field(receiver_role_tag, record_path)
    state = record_state(record, store.get_field(receiver_role_tag, "escalation_records"))
    if state != "forwarded" or record.forward_to != receiver_role_tag:
        raise ValueError(f"{receiver_role_tag}는 전달받은 기록만 되돌릴 수 있음: {record_path}")
    send_back = SendBack(from_role=receiver_role_tag, suspected_causes=[SuspectedCause(type="misrouted")], ts=now_iso())
    store.update_state(
        receiver_role_tag,
        fields=[FieldWrite(f"{record_path}.send_back", send_back)],
        logs=[LogWrite(
            "misrouted_send_back", record.agent_id,
            payload={"from_role": receiver_role_tag, "validation_ts": record.validation.ts if record.validation else None},
        )],
    )


def allocate_validated_forecast(
    store: StateStore, supply_role_tag: str, message: Mapping[str, Any]
) -> AllocationCandidate | None:
    """넘어온 기록을 읽어 `allocation_candidate`를 만든다.

    기록이 지금 "전달됨"이고 `forward_to`가 자기이며 `scenario`가 있을 때만 일한다(사람 대기이거나 늦게 도착한 신호이면 아무것도
    하지 않는다). 자기 카드의 `accepts`가 기록을 낸 agent의 `produces`와 맞지 않으면 자기 일이 아니므로 `misrouted`로 되돌린다.
    """
    record = store.get_field(supply_role_tag, message["field_path"])
    state = record_state(record, store.get_field(supply_role_tag, "escalation_records"))
    if state != "forwarded" or record.forward_to != supply_role_tag or record.scenario is None:
        log(supply_role_tag, "allocate_validated_forecast", agent_id=record.agent_id, skipped=state)
        return None
    if not accepts_record(store.get_field(supply_role_tag, "agent_cards"), supply_role_tag, record.role_tag):
        send_back_misrouted(store, supply_role_tag, message["field_path"])
        return None

    candidate = allocate_forecast_candidate(record.agent_id, record.scenario.value)
    store.update_state(  # 후보안과 그 로그 항목을 한 번의 State 갱신으로 쓴다
        supply_role_tag,
        fields=[FieldWrite("allocation_candidates", [*store.get_field(supply_role_tag, "allocation_candidates"), candidate])],
        logs=[LogWrite("allocation_candidate_generated", record.agent_id,
                       payload={"plan_id": candidate.plan_id, "value": record.scenario.value})],
    )
    return candidate


def process_pending_allocations(store: StateStore, supply_role_tag: str) -> list[AllocationCandidate]:
    """supply_coordination의 채널(자기 role_tag)에 쌓인 "전달됨" 신호를 모두 처리한다. 채널이 비면 끝난다."""
    queue = store.queue(supply_role_tag)
    candidates: list[AllocationCandidate] = []
    while not queue.empty():
        candidate = allocate_validated_forecast(store, supply_role_tag, queue.get_nowait())
        if candidate is not None:
            candidates.append(candidate)
    return candidates


async def _run_guarded(
    store: StateStore,
    forecast_role_tag: str,
    agent_id: str,
    tracker: StageTracker,
    action: Callable[[], Awaitable[T]],
    run_kind: str,
) -> tuple[bool, T | None]:
    """forecast 실행(첫 실행·재실행)의 계산 부분 하나를 돌린다. 예외로 끝나면 재시도 없이 바로 `forecast_run_error`
    escalation 기록과 로그 항목을 쓰고(보류) (False, None)을 반환한다. 그 인스턴스는 멈추고 State의 forecast 기록은 이전 값
    그대로다. State를 쓰는 부분은 여기에 넣지 않는다 — 그 예외는 삼키지 않고 올라간다."""
    try:
        return True, await action()
    except Exception as error:  # 계산 도중의 모든 예외는 시스템 오류로 본다
        text = f"{type(error).__name__}: {error}"
        _hold(  # forecast 기록은 이전 값 그대로 둔다
            store, forecast_role_tag, agent_id, "forecast_run_error",
            f"forecast {run_kind} 실행이 예외로 끝남 — 단계 {tracker.step}: {text}", "forecast_run_error",
            {"run": run_kind, "step": tracker.step, "exception": text},
        )
        return False, None


async def run_forecast_steps_select_and_submit(
    store: StateStore,
    forecast_role_tag: str,
    inputs: InstanceInputs,
    scheduled_promotion: bool = False,
) -> tuple[ForecastStepsResult | None, bool]:
    """내부 단계 1-4와 가정 선택을 계산하고 기록을 한 번 쓰는 첫 실행 진입점.

    계산이 예외로 끝나면 재시도 없이 바로 `forecast_run_error` escalation 기록을 만든다(기록은 이전 값 그대로). 이때 결과는
    (None, False)다. 보류 중인 인스턴스는 실행하지 않는다(결과 (None, False)).
    """
    record_index = _find_forecast_record_index(store, forecast_role_tag, inputs.company_id, inputs.item_id)
    agent_id = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]").agent_id
    if _skip_if_held(store, forecast_role_tag, agent_id, "first_run"):
        return None, False
    tracker = StageTracker()

    async def compute() -> tuple[ForecastStepsResult, _Plan]:
        result = run_forecast_steps(inputs, scheduled_promotion, tracker)
        record = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]")
        tracker.step = "select_assumption"
        plan = _plan_selection(
            store, forecast_role_tag, inputs.company_id, inputs.item_id, result.assumptions,
            no_computable_rationale(result.collection, result.excluded_assumptions), False,
            apply_steps_to_record(record, result),
        )
        return result, plan

    ok, computed = await _run_guarded(store, forecast_role_tag, agent_id, tracker, compute, "first_run")
    if not ok or computed is None:
        return None, False
    result, plan = computed
    return result, _apply_plan(store, forecast_role_tag, plan)


def _previous_values(record: ForecastRecord) -> dict[str, Any]:
    """재실행으로 덮어쓰기 전의 값. `rerun` 로그 항목의 payload에 남긴다."""
    return {
        "assumptions": [a.model_dump(mode="json") for a in record.assumptions],
        "scenario": record.scenario.model_dump(mode="json") if record.scenario is not None else None,
        "selection_basis": record.selection_basis,
        "validation": record.validation.model_dump(mode="json") if record.validation is not None else None,
    }


async def rerun_forecast_steps_select_and_submit(
    store: StateStore,
    forecast_role_tag: str,
    handling: ForecastRerunHandling,
    causes: SuspectedCause | list[SuspectedCause],
    trigger: RerunTrigger,
) -> bool:
    """의심되는 원인 목록을 받아 재개 지점부터 재실행한 결과를 State에 덮어쓰고 가정 선택까지 잇는다. 기록이 "검증 대기"가 되면 True다.

    덮어쓰기 전의 값과 재실행의 계기(`trigger`)는 forecast 로그의 `rerun` 항목 payload에 남기고, 새 기록과 같은 State 갱신에 쓴다. 재실행한 결과는
    아직 검증을 받지 않았으므로 `validation`을 비운다. 가정이 모두 제외되면 재실행 전에 계산된 `scenario`가 있었는지에 따라
    `options_exhausted` 또는 `no_computable_assumption`이다. 계산이 예외로 끝나면 첫 실행과 같이 재시도 없이 바로
    `forecast_run_error` escalation 기록을 만든다(State의 기록은 이전 값 그대로).
    """
    cause_list = [causes] if isinstance(causes, SuspectedCause) else list(causes)
    inputs = handling.inputs
    record_index = _find_forecast_record_index(store, forecast_role_tag, inputs.company_id, inputs.item_id)
    stored = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]")
    if _skip_if_held(store, forecast_role_tag, stored.agent_id, "rerun"):
        return False

    async def compute() -> tuple[_Plan, LogWrite]:
        result = handling.rerun(cause_list)
        record = apply_steps_to_record(stored, result).model_copy(update=_UNVALIDATED)
        rerun_log = LogWrite(
            "rerun", stored.agent_id,
            payload={
                "trigger": trigger, "suspected_cause_reasons": [suspected_cause_reason(c) for c in cause_list],
                "previous": _previous_values(stored),
            },
        )
        rationale = (
            options_exhausted_rationale(result.excluded_assumptions)
            if stored.scenario is not None
            else no_computable_rationale(result.collection, result.excluded_assumptions)
        )
        handling.tracker.step = "select_assumption"
        plan = _plan_selection(
            store, forecast_role_tag, inputs.company_id, inputs.item_id, result.assumptions, rationale, True, record
        )
        return plan, rerun_log

    ok, computed = await _run_guarded(
        store, forecast_role_tag, stored.agent_id, handling.tracker, compute, "rerun"
    )
    if not ok or computed is None:
        return False
    plan, rerun_log = computed
    return _apply_plan(store, forecast_role_tag, plan, [rerun_log])


async def check_forecast_verdict(
    store: StateStore,
    forecast_role_tag: str,
    handling: ForecastRerunHandling,
) -> str:
    """forecast가 기록을 읽고, 지금 상태가 자기 담당("판정됨" 또는 "되돌려짐")일 때만 처리한다(아니면 넘어간다). 처리 결과를 반환한다:
    - `"forwarded"`: `passed`(또는 `misrouted` 반송)라서 다음 agent를 골라 `forward_to`에 썼다.
    - `"next_agent_unresolved"`: 다음 agent를 고르지 못해 `no_next_agent`/`multiple_next_agents` escalation 기록을 만들었다.
    - `"rerun"`: `failed` 판정이나 값 문제 `send_back`의 `suspected_causes` 전체로 한 번 재실행하고 다시 썼다.
    - `"run_error"`: 재실행했지만 예외로 끝나 `forecast_run_error` escalation 기록을 만들었다.
    - `"waiting"`: 자기 담당 상태가 아니거나(검증 대기, 전달됨, 사람 대기, 늦게 도착한 신호), `error` 판정이다.

    forecast의 재실행은 스스로 끝나므로(AGENT_NODE_LIST.md "재실행") 이 경로에서 failed의 반복을 세지 않는다.
    """
    inputs = handling.inputs
    record_index = _find_forecast_record_index(store, forecast_role_tag, inputs.company_id, inputs.item_id)
    record = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]")
    state = record_state(record, store.get_field(forecast_role_tag, "escalation_records"))
    causes: list[SuspectedCause] = []
    trigger: RerunTrigger = "send_back"
    if state == "sent_back" and record.send_back is not None:
        if record.send_back.is_misrouted:  # 재실행하지 않고 다음 agent 선택만 다시 한다
            return select_next_agent(store, forecast_role_tag, inputs.company_id, inputs.item_id)
        causes = record.send_back.suspected_causes
    elif state == "judged" and record.validation is not None:
        if record.validation.status == "passed":
            return select_next_agent(store, forecast_role_tag, inputs.company_id, inputs.item_id)
        causes = record.validation.suspected_causes  # error 판정이면 비어 있다
        trigger = "failed_verdict"
    if not causes:
        return "waiting"
    await rerun_forecast_steps_select_and_submit(store, forecast_role_tag, handling, causes, trigger)
    failed = has_open_escalation(
        store.get_field(forecast_role_tag, "escalation_records"), record.agent_id, "forecast_run_error"
    )
    return "run_error" if failed else "rerun"
