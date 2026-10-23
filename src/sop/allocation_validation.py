"""`allocation_validation`: 배분 결론(`allocations`)의 검증 규칙 (공통 틀은 `validation_agent.py`).

규칙은 하나다: 배분 합계가 그 라인의 공급 가능량(재고 + 생산 중 물량 + 이번 주기 잔여 capacity)을 넘지 않는가. capacity는 생산 쪽
자원이라 supply_coordination이 스스로 확인할 수 없는 agent 경계를 넘는 제약이다. 미납분·계약 하한을 지켰는지는 supply_coordination이
직접 적용한 규칙이라 다시 보지 않는다(계산을 재현하지 않는다). 검증agent는 State와 이번 주기 스냅샷만 읽는다 — 잔여 capacity는
State(`capacity_pools`)에서, 재고·생산 중 물량은 스냅샷에서 읽는다.
"""

from collections.abc import Callable
from pathlib import Path

import pandas as pd

from .access import StateStore
from .external_data import load_data_end, load_inventory, load_line_assignments, load_work_in_progress, primary_line
from .state import Allocation, SuspectedCause
from .validation_agent import ValidationRules

_EPS = 1e-9

# 라인 -> 그 라인 item들의 재고 + 생산 중 물량
StockProvider = Callable[[str], float]


def snapshot_stock_provider(data_dir: str | Path | None = None, data_end: pd.Timestamp | None = None) -> StockProvider:
    """이번 주기 스냅샷에서 라인의 재고와 생산 중 물량 합계를 읽는 함수. 라인 소속은 기준일의 주 라인이다(M4)."""
    on = pd.Timestamp(data_end) if data_end is not None else load_data_end(data_dir)
    assignments = load_line_assignments(data_dir)
    inventory, wip = load_inventory(data_dir), load_work_in_progress(data_dir)

    def stock_of(line_id: str) -> float:
        def on_line(item_id: str) -> bool:
            try:
                return primary_line(assignments, item_id, on) == line_id
            except KeyError:
                return False

        return float(
            inventory[inventory["item_id"].map(on_line)]["quantity"].sum() + wip[wip["item_id"].map(on_line)]["quantity"].sum()
        )

    return stock_of


def remaining_capacity_of(store: StateStore, validator_role_tag: str, line_id: str) -> float:
    for pool in store.get_field(validator_role_tag, "capacity_pools"):
        if pool.line_id == line_id:
            return pool.remaining_capacity
    raise ValueError(f"capacity_pools에 라인 {line_id!r}이 없음")


def allocation_validation_rules(store: StateStore, validator_role_tag: str, stock_of: StockProvider) -> ValidationRules:
    """`allocations` 기록 하나의 검증 규칙. 인스턴스는 라인(`line_id`)이다."""

    def check(record: Allocation) -> list[SuspectedCause]:
        total = sum(entry.quantity for entry in record.entries)
        supply = stock_of(record.line_id) + remaining_capacity_of(store, validator_role_tag, record.line_id)
        return [SuspectedCause(type="allocation", issue="exceeds_supply")] if total > supply + _EPS else []

    return ValidationRules(instance_id=lambda record: record.line_id, check=check, instance_kind="line")
