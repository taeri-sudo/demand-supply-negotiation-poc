"""라인 조율 한 번의 입력을 만든다 (GRAPH_FLOW.md "조율 흐름" 입력 고정).

입력은 두 곳에서 온다.
- 외부 데이터(`ExternalLineData`): 수주, 사람이 정한 수량, 계약 조건, 재고·생산 중 물량, 묶임. 교체 가능한 함수(`LineDataProvider`)로 읽는다
  (AGENT_NODE_LIST.md "외부 경계"). 실제 데이터나 API로 바꿀 때 이 함수만 바꾼다.
- State: 검증을 통과해 넘어온 forecast 기록의 요청량(`scenario.value`), 라인의 잔여 capacity, 사람이 처리해 넣은 하한 수량과 유지할 강한
  묶임 수량(`escalation_records`의 `resolution`).

같은 호출은 같은 입력을 만든다. 조율은 시작할 때 한 번 읽어 고정하고 끝나기 직전에 한 번 더 읽는다(`coordination_flow.py`).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from .access import StateStore
from .coordination_changes import FREEZE_DAYS
from .escalation_records import is_dismissed
from .state import EscalationRecord, Peg
from .supply_allocation_rules import (
    Commitment,
    ForecastDemand,
    Guarantee,
    LineInputs,
    ManualDemand,
    OrderDemand,
    WipLot,
)

LineOf = Callable[[str], str]  # item_id -> 그 item의 라인(라인 기준 정보 `line_assignments`의 주 라인)


@dataclass(frozen=True)
class CoordinationSettings:
    """한 계획 주기의 조율 설정. 스냅샷 기준일과 주기는 주기가 시작될 때 고정된다."""

    cycle: str  # "YYYY-MM"(예측 대상 달)
    as_of: date
    cycle_end: date
    freeze_days: int = FREEZE_DAYS


class ExternalLineData(BaseModel):
    """한 라인의 외부 데이터. `LineInputs` 중 State에서 오지 않는 부분이다."""

    orders: list[OrderDemand] = Field(default_factory=list)
    manual: list[ManualDemand] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    guarantees: list[Guarantee] = Field(default_factory=list)
    work_in_progress: list[WipLot] = Field(default_factory=list)
    units: dict[str, float] = Field(default_factory=dict)
    margins: dict[str, float] = Field(default_factory=dict)
    inventory: float = 0.0
    pegs: dict[str, Peg] = Field(default_factory=dict)


LineDataProvider = Callable[[str], ExternalLineData]  # line_id -> 외부 데이터


def line_forecast_records(store: StateStore, role_tag: str, line_id: str, line_of: LineOf) -> list[tuple[int, str]]:
    """라인에 속한 forecast 인스턴스((기록 번호, agent_id)). 라인은 기록의 item으로 찾는다."""
    return [
        (index, record.agent_id)
        for index, record in enumerate(store.get_field(role_tag, "forecast_records"))
        if line_of(record.item_id) == line_id
    ]


def forwarded_forecasts(store: StateStore, role_tag: str, line_id: str, line_of: LineOf) -> list[ForecastDemand]:
    """라인의 예측 수요: 검증을 통과해(또는 검증 면제 통과해) supply_coordination에게 넘어온("전달됨") forecast 기록의 요청량. 이번 주기에서 뺀 인스턴스는 넣지 않는다."""
    escalations = store.get_field(role_tag, "escalation_records")
    demands: list[ForecastDemand] = []
    for index, agent_id in line_forecast_records(store, role_tag, line_id, line_of):
        record = store.get_field(role_tag, f"forecast_records[{index}]")
        if (
            record.scenario is not None and record.forward_to == role_tag
            and store.record_state(role_tag, f"forecast_records[{index}]") == "forwarded"
            and not is_dismissed(escalations, agent_id=agent_id)
        ):
            demands.append(ForecastDemand(agent_id=agent_id, company_id=record.company_id, item_id=record.item_id, quantity=record.scenario.value))
    return demands


def human_forecast_refs(store: StateStore, role_tag: str) -> frozenset[str]:
    """사람이 escalation을 처리해 요청량을 넣은 forecast 인스턴스의 `agent_id`(`selection_basis: "human"`)."""
    return frozenset(r.agent_id for r in store.get_field(role_tag, "forecast_records") if r.selection_basis == "human")


def resolution_inputs(records: list[EscalationRecord], line_id: str) -> tuple[dict[str, float], dict[str, float]]:
    """라인의 처리된 escalation 기록에서 조율 입력에 반영할 사람의 값: (`floor_overrides`, `peg_limits`).

    `commitments_exceed_capacity`의 값은 (고객사, item) 하한을, `pegged_exceed_capacity`의 값은 강한 묶임 수량을 대체한다. 처리가 끝난 뒤에도
    그 주기의 이후 조율에 계속 적용하고, 같은 `demand_ref`에 값이 여럿이면 나중에 처리한 값이 이긴다.
    """
    floor_overrides: dict[str, float] = {}
    peg_limits: dict[str, float] = {}
    closed = [
        r for r in records
        if r.line_id == line_id and r.status == "resolved" and r.resolution is not None and r.resolution.kind == "value"
        and isinstance(r.resolution.value, dict)
    ]
    for record in sorted(closed, key=lambda r: r.resolution.ts):  # pyright: ignore[reportOptionalMemberAccess]
        values = {ref: float(quantity) for ref, quantity in record.resolution.value.items()}  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
        if record.reason == "commitments_exceed_capacity":
            floor_overrides |= values
        elif record.reason == "pegged_exceed_capacity":
            peg_limits |= values
    return floor_overrides, peg_limits


def remaining_capacity(store: StateStore, role_tag: str, line_id: str) -> float:
    for pool in store.get_field(role_tag, "capacity_pools"):
        if pool.line_id == line_id:
            return pool.remaining_capacity
    raise ValueError(f"capacity_pools에 라인 {line_id!r}이 없음")


def build_line_inputs(
    store: StateStore, role_tag: str, settings: CoordinationSettings, provider: LineDataProvider, line_of: LineOf, line_id: str
) -> LineInputs:
    """라인 하나의 조율 입력. 외부 데이터와 State를 읽어 한 번에 만든다(조율은 이 값을 고정해 쓴다)."""
    data = provider(line_id)
    floor_overrides, peg_limits = resolution_inputs(store.get_field(role_tag, "escalation_records"), line_id)
    return LineInputs(
        line_id=line_id, cycle=settings.cycle, as_of=settings.as_of, cycle_end=settings.cycle_end,
        forecasts=forwarded_forecasts(store, role_tag, line_id, line_of), orders=data.orders, manual=data.manual,
        commitments=data.commitments, guarantees=data.guarantees, work_in_progress=data.work_in_progress, units=data.units,
        margins=data.margins, inventory=data.inventory, remaining_capacity=remaining_capacity(store, role_tag, line_id),
        pegs=data.pegs, floor_overrides=floor_overrides, peg_limits=peg_limits,
    )
