"""supply_coordination 조율 테스트용 도구.

[테스트 전용 입력] 경합 상황은 fixture의 수요·capacity 값으로 만든다(가동률을 바꿔 부족을 만들지 않는다). 계약 단위는 기본 6이다.
"""

from datetime import date

from sop.access import StateStore
from sop.agent_cards import default_agent_cards
from sop.capacity import initial_allocations
from sop.state import CapacityPool, InteractionProtocol, RolePermission, State
from sop.supply_allocation_rules import (
    Commitment,
    ForecastDemand,
    Guarantee,
    LineInputs,
    ManualDemand,
    OrderDemand,
    WipLot,
    unit_key,
)
from sop.supply_coordination_human_manager import commitment_gap_protocol_entry, commitments_exceed_capacity_protocol_entry
from sop.validation_settings import ALLOCATION_VALIDATOR_ROLE_TAG, default_validation_settings

SUPPLY = "supply_coordination"
VALIDATOR = ALLOCATION_VALIDATOR_ROLE_TAG
AS_OF = date(2017, 8, 15)
CYCLE_END = date(2017, 8, 31)


def inputs(
    *, line_id: str = "L1", forecasts=(), orders=(), manual=(), commitments=(), guarantees=(), wip=(), inventory: float = 0.0,
    capacity: float = 0.0, pegs=None, units=None, margins=None,
) -> LineInputs:
    """기본값: 모든 item의 계약 단위 6, 1개당 마진 1.0(`margins`로 item별로 바꾼다)."""
    items = {d.item_id for d in [*forecasts, *orders, *manual]}
    return LineInputs(
        line_id=line_id, cycle="2017-08", as_of=AS_OF, cycle_end=CYCLE_END,
        forecasts=list(forecasts), orders=list(orders), manual=list(manual), commitments=list(commitments),
        guarantees=list(guarantees), work_in_progress=list(wip),
        units={unit_key(None, item): 6.0 for item in items} | (units or {}),
        margins={item: 1.0 for item in items} | (margins or {}),
        inventory=inventory, remaining_capacity=capacity, pegs=pegs or {},
    )


def forecast(agent_id: str, company: str, item: str, quantity: float) -> ForecastDemand:
    return ForecastDemand(agent_id=agent_id, company_id=company, item_id=item, quantity=quantity)


def order(demand_id: str, company: str, item: str, quantity: float, due: date, delivered: float = 0.0) -> OrderDemand:
    return OrderDemand(demand_id=demand_id, company_id=company, item_id=item, quantity=quantity, due_date=due, delivered_quantity=delivered)


def manual(demand_id: str, company: str | None, item: str, quantity: float) -> ManualDemand:
    return ManualDemand(demand_id=demand_id, company_id=company, item_id=item, quantity=quantity)


def commitment(company: str, item: str, total: float, *, binding: bool = True, taken: float = 0.0) -> Commitment:
    """이번 주기(2017-08)부터 2017-12까지 5개월이 남은 약정. 이번 주기 몫 = (total - taken) / 5."""
    return Commitment(
        company_id=company, item_id=item, binding=binding, total_quantity=total, period_start=date(2017, 1, 1),
        period_end=date(2017, 12, 31), taken_quantity=taken,
    )


def guarantee(company: str, item: str, quantity: float) -> Guarantee:
    return Guarantee(company_id=company, item_id=item, quantity=quantity)


def wip_lot(item: str, quantity: float, demand_ref: str) -> WipLot:
    return WipLot(item_id=item, quantity=quantity, demand_ref=demand_ref)


def by_ref(entries) -> dict[tuple[str, str], float]:
    """(demand_ref, peg) → 배분 수량."""
    return {(e.demand_ref, e.peg): e.quantity for e in entries}


_PERMISSIONS = [
    (SUPPLY, "allocations", "r"), (SUPPLY, "allocations", "w"), (SUPPLY, "escalation_records", "r"),
    (SUPPLY, "escalation_records", "a"), (SUPPLY, "role_logs", "r"), (SUPPLY, "role_logs.supply_coordination", "a"),
    (SUPPLY, "interaction_protocol", "r"), (SUPPLY, "capacity_pools", "r"), (SUPPLY, "agent_cards", "r"),
    (VALIDATOR, "allocations", "r"), (VALIDATOR, "allocations[*].validation", "w"), (VALIDATOR, "capacity_pools", "r"),
    (VALIDATOR, "escalation_records", "r"), (VALIDATOR, "escalation_records", "a"), (VALIDATOR, "role_logs", "r"),
    (VALIDATOR, f"role_logs.{VALIDATOR}", "a"),
    ("system_operator", "escalation_records", "r"), ("system_operator", "escalation_records", "w"), ("system_operator", "allocations", "r"),
    ("system_operator", "allocations[*].validation", "w"), ("system_operator", "role_logs", "r"), ("system_operator", "role_logs.system_operator", "a"),
    ("production_plan", "capacity_pools", "r"), ("production_plan", "capacity_pools", "w"),  # M5의 production_plan을 대신하는 역할
]


def make_store(
    lines: tuple[str, ...] = ("L1", "L2"), capacity: float = 10000.0, protocol: list[InteractionProtocol] | None = None,
    produces: str | None = None,
) -> StateStore:
    """라인마다 빈 `allocations` 기록과 capacity 풀이 있는 store. 기본 protocol은 약정 차이 알림과 하한 초과 항목이다."""
    pools = [CapacityPool(line_id=line, total_capacity=capacity, remaining_capacity=capacity) for line in lines]
    cards = [c.model_copy(update={"produces": produces}) if c.role_tag == SUPPLY else c for c in default_agent_cards()]
    entries = [commitment_gap_protocol_entry(), commitments_exceed_capacity_protocol_entry()] if protocol is None else protocol
    return StateStore(
        State(
            capacity_pools=pools, allocations=initial_allocations(pools), interaction_protocol=entries, agent_cards=cards,
            validation_settings=default_validation_settings(),
            role_permissions=[RolePermission(role_tag=r, field_path=f, access=a) for r, f, a in _PERMISSIONS],  # pyright: ignore[reportArgumentType]
        )
    )


def pending(store: StateStore) -> dict[str, int]:
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


def drain(store: StateStore) -> None:
    for queue in store._queues.values():  # pyright: ignore[reportPrivateUsage]
        while not queue.empty():
            queue.get_nowait()
