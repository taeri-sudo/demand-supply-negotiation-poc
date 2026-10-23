"""조율 흐름: 정기 조율을 시작하는 때, 이어지는 조율의 배분, 입력 고정과 결론 직전의 확인 (GRAPH_FLOW.md "조율 흐름").

- 정기 조율 시작 규칙(`RegularCoordinationStartRule`)은 교체 가능한 class다(같은 입출력, 구현체만 교체). 기본 규칙(`NoProcessingInstances`)은 그 라인의
  forecast 인스턴스 중 처리 중인 것이 없으면 시작한다.
- 이어지는 조율(`continuation_judgment`)은 변경이 닿는 item의 약한 묶임만 다시 나누고 나머지는 이전 결론 그대로 고정한다
  (AGENT_NODE_LIST.md supply_coordination "이어지는 조율").
- `coordinate_line`은 입력을 고정해 계산하고, 결론을 쓰기 직전에 입력을 한 번 더 읽어 그 사이 들어온 변경을 전부 마지막 순서로 얹은 뒤 결론을
  한 번 쓴다. 확인을 반복하지 않으므로 확인 뒤 들어온 값은 바로 이어지는 조율로 넘어간다.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from .access import StateStore
from .coordination_changes import Change, diff_inputs, touched_items
from .coordination_inputs import (
    CoordinationSettings,
    LineDataProvider,
    LineOf,
    build_line_inputs,
    human_forecast_refs,
    line_forecast_records,
)
from .escalation_records import expired_without_demand, is_dismissed
from .judgment import StructuredJudgment
from .logging_utils import log
from .record_state import HOLD_STATES
from .state import Allocation
from .supply_allocation_rules import LineInputs, allocate_line, entries_of
from .supply_coordination_line import CoordinationKind, CoordinationResult, exceed_mode, find_allocation_index, run_line_coordination

InstanceKind = Literal["processing", "delivered", "excluded"]
ComputeDelay = Callable[[str], Awaitable[None]]  # 계산이 오래 걸리는 구간(M7의 LLM 응답 대기)을 대신하는 await 지점


@dataclass(frozen=True)
class InstanceStatus:
    """라인에 속한 forecast 인스턴스 하나가 정기 조율에서 어떻게 다뤄지는지.

    - `processing`: 상태 없음(아직 실행 안 함), 검증 대기, 판정됨, 되돌려짐. 재실행 중인 인스턴스도 여기에 든다.
    - `delivered`: 전달됨(끝남). 그 값이 이번 조율에 들어간다.
    - `excluded`: 기다리지 않는다. 업무 대기·시스템 대기(몫도 비워 두지 않는다. 처리되면 이어지는 조율로 들어온다), 시한 초과로 예측 수요 없이
      끝난 인스턴스, `dismiss`로 이번 주기에서 뺀 인스턴스.
    """

    agent_id: str
    kind: InstanceKind
    reason: str


class RegularCoordinationStartRule(Protocol):
    """그 라인의 정기 조율을 지금 시작할지 정하는 규칙. 구현체를 바꿔도 입출력은 같다."""

    def ready(self, line_id: str, instances: Sequence[InstanceStatus]) -> bool: ...


class NoProcessingInstances:
    """기본 규칙: 그 라인의 forecast 인스턴스 중 처리 중인 것이 없으면 시작한다."""

    def ready(self, line_id: str, instances: Sequence[InstanceStatus]) -> bool:
        return all(instance.kind != "processing" for instance in instances)


def instance_statuses(store: StateStore, role_tag: str, line_id: str, line_of: LineOf) -> list[InstanceStatus]:
    escalations = store.get_field(role_tag, "escalation_records")
    statuses: list[InstanceStatus] = []
    for index, agent_id in line_forecast_records(store, role_tag, line_id, line_of):
        path = f"forecast_records[{index}]"
        record = store.get_field(role_tag, path)
        state = store.record_state(role_tag, path)
        if is_dismissed(escalations, agent_id=agent_id):
            status = InstanceStatus(agent_id, "excluded", "dismissed")
        elif state in HOLD_STATES:
            status = InstanceStatus(agent_id, "excluded", state)
        elif record.scenario is None and expired_without_demand(escalations, agent_id):
            status = InstanceStatus(agent_id, "excluded", "expired_without_demand")
        elif state in ("forwarded", "completed"):
            status = InstanceStatus(agent_id, "delivered", state)
        else:
            status = InstanceStatus(agent_id, "processing", state)
        statuses.append(status)
    return statuses


def decided_inputs(store: StateStore, role_tag: str, allocation: Allocation) -> LineInputs | None:
    """라인의 마지막 결론을 조율할 때 고정한 입력. 조율한 적이 없으면 None이다. 입력은 그 조율 로그(`allocation_decided`)에 남아 있다."""
    if allocation.log_seq is None:
        return None
    entry = next((e for e in store.role_log(role_tag, role_tag) if e.seq == allocation.log_seq), None)
    if entry is None or "inputs" not in entry.payload:
        raise ValueError(f"{allocation.line_id}의 log_seq({allocation.log_seq})가 가리키는 조율 로그 항목이 없음")
    return LineInputs.model_validate(entry.payload["inputs"])


def continuation_judgment(
    inputs: LineInputs, previous: Allocation, changes: Sequence[Change], mode: Literal["intervention", "notice"]
) -> StructuredJudgment:
    """이어지는 조율의 배분. 변경이 닿는 item의 약한 묶임만 다시 나누고, 강한 묶임과 닿지 않는 item의 약한 묶임은 이전 결론 수량으로 고정한다.

    영향 범위를 알 수 없는 변경이 있거나 변경이 없으면(`touched_items`가 None) 고정하지 않고 약한 묶임 전체를 다시 나눈다.
    """
    items = touched_items(list(changes)) if changes else None
    frozen = (
        {}
        if items is None
        else {(e.demand_ref, e.peg): e.quantity for e in previous.entries if e.peg == "soft" and e.item_id not in items}
    )
    judgment = allocate_line(inputs, mode, frozen)
    judgment.judgment["touched_items"] = sorted(items) if items is not None else None
    return judgment


async def coordinate_line(
    store: StateStore,
    role_tag: str,
    line_id: str,
    *,
    settings: CoordinationSettings,
    provider: LineDataProvider,
    line_of: LineOf,
    continuing: bool,
    compute_delay: ComputeDelay | None = None,
) -> CoordinationResult:
    """라인 하나를 조율한다. 정기 조율(`continuing=False`)은 그 라인의 수요 전부를 나누고, 이어지는 조율은 마지막 결론 이후의 변경만 다시 나눈다.

    1. 입력을 읽어 고정하고 그 값으로만 계산한다. 계산하는 동안(`compute_delay`) 들어오는 값에는 끼어들지 않는다.
    2. 결론을 쓰기 직전에 입력을 한 번 더 읽는다. 그 사이 바뀐 변경은 전부 마지막 순서로 얹는다(이어지는 조율과 같은 방식).
    3. 결론을 한 번 쓴다. 쓴 입력은 확인에서 읽은 값이라, 확인 뒤 들어온 값은 마지막 결론과의 차이로 남아 바로 이어지는 조율로 간다.
    """
    allocation = store.get_field(role_tag, f"allocations[{find_allocation_index(store, role_tag, line_id)}]")
    protocol = store.get_field(role_tag, "interaction_protocol")
    mode = exceed_mode(protocol)
    human = human_forecast_refs(store, role_tag)
    fixed = build_line_inputs(store, role_tag, settings, provider, line_of, line_id)
    if continuing:
        previous = decided_inputs(store, role_tag, allocation)
        changes = diff_inputs(previous, fixed, human, settings.freeze_days) if previous is not None else []
        judgment = continuation_judgment(fixed, allocation, changes, mode)
        kind: CoordinationKind = "continuing"
    else:
        judgment = allocate_line(fixed, mode)
        kind = "regular"
    inputs, late_changes = fixed, []
    if compute_delay is not None:
        await compute_delay(line_id)
    if judgment.judgment["outcome"] == "allocated":
        fresh = build_line_inputs(store, role_tag, settings, provider, line_of, line_id)
        late_changes = diff_inputs(fixed, fresh, human_forecast_refs(store, role_tag), settings.freeze_days)
        if late_changes:
            overlay = Allocation(line_id=line_id, cycle=settings.cycle, entries=entries_of(judgment))
            judgment = continuation_judgment(fresh, overlay, late_changes, mode)
            inputs = fresh
    demands = {f.agent_id for f in inputs.forecasts} | {o.demand_id for o in inputs.orders} | {m.demand_id for m in inputs.manual}
    ignored = sorted((inputs.floor_overrides.keys() | inputs.peg_limits.keys()) - demands)  # 처리 뒤 수요가 사라진 사람의 값은 무시하고 로그에 남긴다
    result = run_line_coordination(
        store, role_tag, inputs, protocol, judgment=judgment, kind=kind, late_changes=[c.to_payload() for c in late_changes],
        ignored_refs=ignored,
    )
    log(role_tag, "coordinate_line", line_id=line_id, kind=kind, outcome=result.outcome, late_changes=len(late_changes))
    return result

