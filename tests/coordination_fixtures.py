"""조율 흐름 테스트용 도구: forecast 인스턴스, 외부 데이터 제공자, supply_coordination 에이전트를 한 store에 묶는다.

[테스트 전용 입력] 라인과 item 소속, 수요·capacity·계약 조건은 fixture 값이다(가동률을 바꿔 부족을 만들지 않는다). 계약 단위는 6, 1개당 마진은 모두
같다. 외부 데이터는 `Data`가 들고 있는 값이고 테스트가 바꾼다(sales_channel의 새 수주가 들어오는 것을 대신한다).
"""

from collections.abc import Callable
from datetime import datetime

from sop.access import FieldWrite, StateStore
from sop.agent_cards import default_agent_cards
from sop.allocation_validation import allocation_validation_rules
from sop.capacity import initial_allocations
from sop.coordination_inputs import CoordinationSettings, ExternalLineData
from sop.forecast_supply_allocation import forward_validated_forecast
from sop.state import CapacityPool, ForecastRecord, RolePermission, Scenario, State, ValidationResult
from sop.supply_allocation_rules import unit_key
from sop.supply_coordination_agent import LineCheckResult, SupplyCoordinator
from sop.supply_coordination_human_manager import commitment_gap_protocol_entry, commitments_exceed_capacity_protocol_entry
from sop.validation_agent import process_pending_validations
from sop.validation_settings import ALLOCATION_VALIDATOR_ROLE_TAG, default_validation_settings
from supply_fixtures import _PERMISSIONS as SUPPLY_PERMISSIONS  # pyright: ignore[reportPrivateUsage]
from validation_fixtures import FORECAST, HUMAN, SUPPLY, SYSTEM, permissions

VALIDATOR = ALLOCATION_VALIDATOR_ROLE_TAG
SCHEDULER = "scheduler"
SETTINGS = CoordinationSettings(cycle="2017-08", as_of=datetime(2017, 8, 15).date(), cycle_end=datetime(2017, 8, 31).date())
LINES = {"I1": "L1", "I2": "L1", "I3": "L2"}  # item → 라인
ITEMS = tuple(LINES)


def line_of(item_id: str) -> str:
    return LINES[item_id]


class Data:
    """라인별 외부 데이터. `set`으로 바꾸고 `__call__`이 제공자(`LineDataProvider`)다. `on_read`는 N번째 읽기 직전에 부르는 함수다(조율 중 들어오는 값)."""

    def __init__(self) -> None:
        self.by_line: dict[str, dict] = {}
        self.reads = 0
        self.on_read: dict[int, Callable[[], None]] = {}
        self.failures = 0  # 앞으로 읽기가 실패할 횟수

    def set(self, line_id: str, **fields) -> None:
        self.by_line.setdefault(line_id, {}).update(fields)

    def add(self, line_id: str, field: str, *values) -> None:
        self.by_line.setdefault(line_id, {}).setdefault(field, [])
        self.by_line[line_id][field] = [*self.by_line[line_id][field], *values]

    def __call__(self, line_id: str) -> ExternalLineData:
        self.reads += 1
        if self.reads in self.on_read:
            self.on_read[self.reads]()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("강제로 낸 외부 데이터 읽기 실패")
        fields = self.by_line.get(line_id, {})
        return ExternalLineData(
            units={unit_key(None, item): 6.0 for item in ITEMS}, margins={item: 1.0 for item in ITEMS}, **fields,
        )

    def stock(self, line_id: str) -> float:
        """검증agent가 읽는 라인의 재고 + 생산 중 물량."""
        fields = self.by_line.get(line_id, {})
        return float(fields.get("inventory", 0.0)) + sum(lot.quantity for lot in fields.get("work_in_progress", []))


def make_store(
    instances: tuple[tuple[str, str], ...] = (("A", "I1"), ("B", "I1"), ("A", "I2"), ("A", "I3")),
    capacity: dict[str, float] | None = None,
    **store_kwargs,
) -> StateStore:
    """(고객사, item) 인스턴스마다 forecast 기록이 있고 라인마다 빈 `allocations` 기록과 capacity 풀이 있는 store."""
    capacity = capacity or {"L1": 10000.0, "L2": 10000.0}
    pools = [CapacityPool(line_id=line, total_capacity=value, remaining_capacity=value) for line, value in capacity.items()]
    extra = [
        (SUPPLY, "forecast_records", "r"), (SUPPLY, "forecast_records[*].send_back", "w"), (SUPPLY, "escalation_records", "r"),
        (SCHEDULER, "escalation_records", "r"), (SCHEDULER, "role_logs", "r"),
        (HUMAN, "allocations", "r"), (SYSTEM, "capacity_pools", "r"),
    ]
    all_permissions = [
        *permissions(),
        *(RolePermission(role_tag=r, field_path=f, access=a) for r, f, a in [*SUPPLY_PERMISSIONS, *extra]),  # pyright: ignore[reportArgumentType]
    ]
    state = State(
        forecast_records=[ForecastRecord(agent_id=f"{c}:{i}", company_id=c, item_id=i) for c, i in instances],
        capacity_pools=pools, allocations=initial_allocations(pools),
        interaction_protocol=[commitment_gap_protocol_entry(), commitments_exceed_capacity_protocol_entry()],
        agent_cards=default_agent_cards(), validation_settings=default_validation_settings(), role_permissions=all_permissions,
    )
    return StateStore(state, **store_kwargs)


def coordinator(store: StateStore, data: Data, **kwargs) -> SupplyCoordinator:
    return SupplyCoordinator(store, SETTINGS, data, line_of, **kwargs)


def record_index(store: StateStore, company: str, item: str) -> int:
    return next(i for i, r in enumerate(store.state.forecast_records) if (r.company_id, r.item_id) == (company, item))


def deliver(store: StateStore, company: str, item: str, value: float, basis: str = "rule") -> None:
    """forecast가 검증을 통과한(또는 검증 면제 통과한) 요청량을 supply_coordination에게 넘긴 상태("전달됨")로 쓴다. 신호는 자동으로 간다."""
    index = record_index(store, company, item)
    exempt = basis in ("human", "expiry")
    delivered = store.state.forecast_records[index].model_copy(
        update={
            "scenario": Scenario(value=value, assumption_ids=[] if exempt else ["a"], derivation=None if exempt else "chosen"),
            "selection_basis": basis, "validation": None if exempt else ValidationResult(status="passed", ts="2017-08-15T00:00:00+00:00"),
            "forward_to": SUPPLY, "send_back": None,
        }
    )
    store.update_state(FORECAST, fields=[FieldWrite(f"forecast_records[{index}]", delivered)])


def forward_after_resolution(store: StateStore, company: str, item: str) -> None:
    """사람이 처리해 "판정됨"이 된 forecast 기록을 forecast가 supply_coordination에게 넘긴다."""
    assert forward_validated_forecast(store, FORECAST, company, item)


async def pump(agent: SupplyCoordinator) -> list[LineCheckResult]:
    """supply_coordination 채널의 신호를 모두 처리하고(확인까지) 라인 확인 결과를 순서대로 돌려준다."""
    queue = agent.store.queue(agent.role_tag)
    results: list[LineCheckResult] = []
    while not queue.empty():
        message = queue.get_nowait()
        results += await agent.handle_signal(message)
        queue.ack(message)
    return results


def validate(store: StateStore, data: Data) -> list[ValidationResult]:
    """`allocation_validation`이 신호가 온 결론을 모두 판정한다."""
    return process_pending_validations(store, VALIDATOR, {SUPPLY: allocation_validation_rules(store, VALIDATOR, data.stock)})


def decided(store: StateStore) -> list:
    return [e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_decided"]


def entries_by_ref(store: StateStore, line_id: str) -> dict[tuple[str, str], float]:
    allocation = next(a for a in store.state.allocations if a.line_id == line_id)
    return {(e.demand_ref, e.peg): e.quantity for e in allocation.entries}


def line_state(store: StateStore, line_id: str) -> str:
    index = next(i for i, a in enumerate(store.state.allocations) if a.line_id == line_id)
    return store.record_state(SUPPLY, f"allocations[{index}]")
