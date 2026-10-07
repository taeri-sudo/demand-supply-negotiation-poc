"""forecast agent → supply_coordination agent 단방향 최적화 배분 (M1).

GRAPH_FLOW.md "상호작용 세 가지 유형"의 "최적화"에 해당하는 정방향 edge —
forecast가 정한 최종 요청량(시나리오)을 그대로 담아 supply_coordination이
`allocation_candidate` 하나를 생성한다. 라운드도, capacity 비교도, 응답
대기도 없다(우선순위 점수 산출은 (회사,item) 인스턴스가 1개뿐이라 경쟁
자체가 없어 이 마일스톤에서는 값을 그대로 반영하는 것으로 근사 — 실제
다인스턴스 경쟁 로직은 M4).

역방향(supply_coordination→forecast, plan agent의 infeasible이 트리거하는
send-back)은 plan agent 자체가 아직 없어(M5) 이 모듈에서 다루지 않는다.

`run_forecast_select_and_allocate`가 가정 선택
(`forecast_assumption_selection.py`) → 이 배분 생성까지 잇는 상위 진입점이다.
`run_forecast_steps_select_and_allocate`는 내부 단계 1-4(`forecast_steps.py`)부터 시작하는 첫 실행이고,
`rerun_forecast_steps_select_and_allocate`는 send-back을 받아 재실행한 결과를 같은 경로로 이어 보낸다.
forecast_records 인스턴스는 리스트 순서가 아니라 (company_id, item_id)로
식별한다(STATE_SCHEMA.md "forecast_records[]" — agent_id는 표시용 합성키일
뿐 조회 키가 아니다) — `_find_forecast_record_index`가 그 조회를 담당한다.
"""

from .access import StateStore
from .forecast_assumption_selection import select_forecast_assumption
from .external_data import InstanceInputs
from .forecast_human_manager import (
    has_open_escalation,
    no_computable_rationale,
    open_forecast_escalation,
    options_exhausted_rationale,
)
from .forecast_steps import ForecastSendBackHandling, ForecastStepsResult, apply_steps_to_record, run_forecast_steps
from .ids import new_id, now_iso
from .state import AllocationCandidate, Assumption, NegotiationLogEntry, Scenario, SuspectedCause, send_back_reason


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


def _append_log(store: StateStore, role_tag: str, event: str) -> None:
    log_entries = store.get_field(role_tag, "negotiation_log")
    store.set_field(
        role_tag,
        "negotiation_log",
        [*log_entries, NegotiationLogEntry(role_tag=role_tag, event=event, ts=now_iso())],
        notify_channel="negotiation_log",
    )


async def run_forecast_select_and_allocate(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    company_id: str,
    item_id: str,
    assumptions: list[Assumption],
    no_assumption_rationale: str | None = None,
    after_send_back: bool = False,
) -> AllocationCandidate | None:
    """가정들에서 forecast가 최종 요청량(시나리오)을 정한 뒤, 그 값을 그대로
    담아 supply_coordination이 `allocation_candidate`를 생성하는 M1 진입점
    (단방향 최적화, 라운드 없음). 인스턴스는 (company_id, item_id)로 지정한다.

    supply_coordination으로 보내지 않고 None을 반환하는 경우(AGENT_NODE_LIST.md "사람 escalation 세 경우"의
    보류 규칙):
    - 계산된 가정이 하나도 없으면(`assumptions`가 비어 있음) `scenario`와 `selection_basis`를 `null`로 두고
      `no_computable_assumption` escalation 기록을 만든다. `no_assumption_rationale`이 그 설명이다. send-back을
      받아 재실행한 결과(`after_send_back`)이고 send-back 전에 계산된 `scenario`가 있었으면 그 값을 그대로 두고
      `options_exhausted` escalation 기록을 만든다(`scenario`가 없었으면 `no_computable_assumption`이다).
    - 가정 선택 ③이면 규칙이 낸 중간값을 `scenario`에 두고(`selection_basis`는 `"rule"`) `selection_unresolved`
      escalation 기록을 만든다.
    - 이 인스턴스에 처리되지 않은(open) escalation 기록이 이미 있으면 새로 정한 요청량도 보내지 않는다.
    """
    record_index = _find_forecast_record_index(store, forecast_role_tag, company_id, item_id)
    record = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]")

    if not assumptions and after_send_back and record.scenario is not None:
        open_forecast_escalation(
            store, forecast_role_tag, record.agent_id, "options_exhausted",
            no_assumption_rationale or "send-back 재실행으로 가정이 모두 제외됨",
        )
        _append_log(store, forecast_role_tag, "scenario_options_exhausted")
        return None

    if not assumptions:
        store.set_field(
            forecast_role_tag,
            f"forecast_records[{record_index}]",
            record.model_copy(update={"assumptions": [], "scenario": None, "selection_basis": None}),
            notify_channel="forecast_records",
        )
        open_forecast_escalation(
            store, forecast_role_tag, record.agent_id, "no_computable_assumption",
            no_assumption_rationale or "계산된 가정이 하나도 없어 요청량을 만들 수 없음",
        )
        _append_log(store, forecast_role_tag, "scenario_not_computable")
        return None

    selection = select_forecast_assumption(assumptions)
    scenario = Scenario(**selection.judgment["scenario"])

    updated_record = record.model_copy(
        update={"assumptions": assumptions, "scenario": scenario, "selection_basis": "rule"}
    )
    store.set_field(
        forecast_role_tag,
        f"forecast_records[{record_index}]",
        updated_record,
        notify_channel="forecast_records",
    )
    _append_log(store, forecast_role_tag, f"scenario_decided_{scenario.derivation}")

    if selection.judgment["escalate"]:
        open_forecast_escalation(
            store, forecast_role_tag, record.agent_id, "selection_unresolved", selection.reasoning
        )
        return None
    if has_open_escalation(store.get_field(forecast_role_tag, "escalation_records"), record.agent_id):
        return None

    candidate = allocate_forecast_candidate(record.agent_id, scenario.value)
    store.set_field(
        supply_role_tag,
        "allocation_candidates",
        [*store.get_field(supply_role_tag, "allocation_candidates"), candidate],
        notify_channel="allocation_candidates",
    )
    _append_log(store, supply_role_tag, f"allocation_candidate_generated_{candidate.plan_id}")

    return candidate


async def run_forecast_steps_select_and_allocate(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    inputs: InstanceInputs,
    scheduled_promotion: bool = False,
) -> tuple[ForecastStepsResult, AllocationCandidate | None]:
    """내부 단계 1-4를 실행하고 그 결과를 State에 반영한 뒤, 가정 선택과 배분까지 잇는 첫 실행 진입점."""
    result = run_forecast_steps(inputs, scheduled_promotion)
    record_index = _find_forecast_record_index(store, forecast_role_tag, inputs.company_id, inputs.item_id)
    record = store.get_field(forecast_role_tag, f"forecast_records[{record_index}]")
    store.set_field(
        forecast_role_tag, f"forecast_records[{record_index}]", apply_steps_to_record(record, result),
        notify_channel="forecast_records",
    )
    candidate = await run_forecast_select_and_allocate(
        store, forecast_role_tag, supply_role_tag, inputs.company_id, inputs.item_id, result.assumptions,
        no_computable_rationale(result.collection, result.excluded_assumptions),
    )
    return result, candidate


async def rerun_forecast_steps_select_and_allocate(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    handling: ForecastSendBackHandling,
    cause: SuspectedCause,
) -> AllocationCandidate | None:
    """send-back을 받아 재개 지점부터 재실행한 결과를 State에 덮어쓰고, 가정 선택과 배분까지 잇는다.

    재실행한 결과는 아직 검증을 받지 않았으므로 `validation`을 비운다. 가정이 모두 제외되면 send-back 전에
    계산된 `scenario`가 있었는지에 따라 `options_exhausted` 또는 `no_computable_assumption`이다.
    """
    inputs = handling.inputs
    result = handling.rerun(cause)
    record_index = _find_forecast_record_index(store, forecast_role_tag, inputs.company_id, inputs.item_id)
    record = apply_steps_to_record(
        store.get_field(forecast_role_tag, f"forecast_records[{record_index}]"), result
    ).model_copy(update={"validation": None})
    store.set_field(
        forecast_role_tag, f"forecast_records[{record_index}]", record, notify_channel="forecast_records"
    )
    _append_log(store, forecast_role_tag, f"send_back_rerun_{send_back_reason(cause)}")
    rationale = (
        options_exhausted_rationale(result.excluded_assumptions)
        if record.scenario is not None
        else no_computable_rationale(result.collection, result.excluded_assumptions)
    )
    return await run_forecast_select_and_allocate(
        store, forecast_role_tag, supply_role_tag, inputs.company_id, inputs.item_id, result.assumptions,
        rationale, after_send_back=True,
    )
