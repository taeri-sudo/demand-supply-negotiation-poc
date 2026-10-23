"""조율 입력의 변경: 마지막으로 조율한 입력과 지금 입력의 차이 (GRAPH_FLOW.md "조율 흐름").

변경은 따로 모아 두지 않고 입력의 차이로 계산한다. 마지막 조율의 입력은 그 조율 로그(`allocation_decided`)의 `inputs`에 남아 있어서
(`allocations[i].log_seq`가 가리킨다) 정기 조율 전의 변경, 조율 중에 들어온 값, 모아 두었다가 하루 한 번 하는 변경이 모두 같은 차이다.

차이 항목마다 영향받는 item(`items`, None이면 영향 범위를 알 수 없어 모든 item)과 조율 시점을 정하는 분류를 단다.
- 사람의 처리 결과·사람이 정한 수량(`human`)과 급한 변경(`urgent`)은 진행 중인 조율이 끝나는 즉시 조율한다.
  급한 변경은 납기가 변경 동결 구간 안(스냅샷 기준일부터 `freeze_days`일)이거나, 그 (고객사, item)의 미납분이나 실제 적용되는 계약 하한
  (이번 주기 수요로 제한한 값)을 바꾸는 변경이다. 약정·공급 보장 물량 자체의 변경도 급한 변경이다.
- 그 밖의 자동 변경은 하루 한 번 정해진 시각에 조율한다.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .supply_allocation_rules import LineInputs, effective_floors, overdue_backlog

FREEZE_DAYS = 7  # 변경 동결 구간(일). 설정값, 기본 7일

# 영향 범위를 알 수 없는 입력 필드: 공급·계약·묶임. 바뀌면 모든 item의 약한 묶임을 다시 나눈다
_SUPPLY_SIDE_FIELDS = ("commitments", "guarantees", "work_in_progress", "units", "margins", "inventory", "remaining_capacity", "pegs")
# item(또는 고객사와 item)마다 값을 가진 필드: 키가 더해지거나 빠지는 것은 수요가 늘고 줄어서 생기는 일이다
_PER_ITEM_FIELDS = frozenset(["units", "margins"])
# 바뀌면 계약 하한에 영향을 주는 필드
_FLOOR_FIELDS = frozenset(["commitments", "guarantees"])


@dataclass(frozen=True)
class Change:
    kind: str  # forecast, order, manual, floor_override, peg_limit, 또는 바뀐 입력 필드 이름
    ref: str  # 바뀐 수요의 demand_ref. 수요가 아닌 필드이면 필드 이름
    items: frozenset[str] | None  # 영향받는 item. None이면 모든 item
    urgent: bool
    human: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "ref": self.ref, "items": sorted(self.items) if self.items is not None else None,
            "urgent": self.urgent, "human": self.human,
        }


def immediate(changes: list[Change]) -> bool:
    """진행 중인 조율이 끝나는 즉시 조율할 변경이 있는가(사람의 처리 결과, 사람이 정한 수량, 급한 변경)."""
    return any(change.urgent or change.human for change in changes)


def touched_items(changes: list[Change]) -> frozenset[str] | None:
    """변경이 닿는 item 전체. 영향 범위를 알 수 없는 변경이 하나라도 있으면 None(모든 item)."""
    items: set[str] = set()
    for change in changes:
        if change.items is None:
            return None
        items |= change.items
    return frozenset(items)


def _affected_keys(previous: LineInputs, current: LineInputs) -> set[tuple[str | None, str]]:
    """미납분이나 실제 적용되는 계약 하한이 이전 입력과 달라진 (고객사, item)."""
    keys: set[tuple[str | None, str]] = set()
    floors = (effective_floors(previous), effective_floors(current))
    keys |= {key for key in floors[0].keys() | floors[1].keys() if floors[0].get(key, 0.0) != floors[1].get(key, 0.0)}
    backlog = (overdue_backlog(previous), overdue_backlog(current))
    return keys | {key for key in backlog[0].keys() | backlog[1].keys() if backlog[0].get(key, 0.0) != backlog[1].get(key, 0.0)}


def _demand_items(inputs: LineInputs) -> dict[str, tuple[str | None, str]]:
    """`demand_ref` → (고객사 또는 None, item)."""
    owners: dict[str, tuple[str | None, str]] = {f.agent_id: (f.company_id, f.item_id) for f in inputs.forecasts}
    owners |= {o.demand_id: (o.company_id, o.item_id) for o in inputs.orders}
    owners |= {m.demand_id: (m.company_id, m.item_id) for m in inputs.manual}
    return owners


def diff_inputs(previous: LineInputs, current: LineInputs, human_forecasts: frozenset[str] = frozenset(), freeze_days: int = FREEZE_DAYS) -> list[Change]:
    """`previous`(마지막 조율의 입력)에서 `current`로 바뀐 것. 같으면 빈 목록이다.

    `human_forecasts`는 사람이 escalation을 처리해 넣은 요청량(`selection_basis: "human"`)의 forecast `agent_id`다. 스냅샷 기준일과 주기는
    주기가 시작될 때 고정되므로 차이로 보지 않는다.
    """
    changes: list[Change] = []
    affected = _affected_keys(previous, current)
    window_end = current.as_of + timedelta(days=freeze_days)

    previous_forecasts = {f.agent_id: f for f in previous.forecasts}
    current_forecasts = {f.agent_id: f for f in current.forecasts}
    for ref in sorted(previous_forecasts.keys() | current_forecasts.keys()):
        before, after = previous_forecasts.get(ref), current_forecasts.get(ref)
        if before == after:
            continue
        sides = [f for f in (before, after) if f is not None]
        changes.append(
            Change(
                "forecast", ref, frozenset(f.item_id for f in sides),
                urgent=any((f.company_id, f.item_id) in affected for f in sides), human=ref in human_forecasts,
            )
        )

    previous_orders = {o.demand_id: o for o in previous.orders}
    current_orders = {o.demand_id: o for o in current.orders}
    for ref in sorted(previous_orders.keys() | current_orders.keys()):
        before, after = previous_orders.get(ref), current_orders.get(ref)
        if before == after:
            continue
        sides = [o for o in (before, after) if o is not None]
        urgent = any(o.due_date <= window_end or (o.company_id, o.item_id) in affected for o in sides)
        changes.append(Change("order", ref, frozenset(o.item_id for o in sides), urgent=urgent, human=False))

    previous_manual = {m.demand_id: m for m in previous.manual}
    current_manual = {m.demand_id: m for m in current.manual}
    for ref in sorted(previous_manual.keys() | current_manual.keys()):
        before, after = previous_manual.get(ref), current_manual.get(ref)
        if before != after:
            changes.append(Change("manual", ref, frozenset(m.item_id for m in (before, after) if m is not None), urgent=False, human=True))

    owners = _demand_items(previous) | _demand_items(current)
    for kind, before_map, after_map in (
        ("floor_override", previous.floor_overrides, current.floor_overrides),
        ("peg_limit", previous.peg_limits, current.peg_limits),
    ):
        for ref in sorted(before_map.keys() | after_map.keys()):
            if before_map.get(ref) != after_map.get(ref):
                owner = owners.get(ref)
                changes.append(Change(kind, ref, frozenset([owner[1]]) if owner is not None else None, urgent=False, human=True))

    for name in _SUPPLY_SIDE_FIELDS:
        before, after = getattr(previous, name), getattr(current, name)
        if name in _PER_ITEM_FIELDS:  # 새 수요가 들어오며 항목이 더해지거나 빠지는 것은 변경이 아니다. 이미 있던 항목의 값이 바뀌어야 변경이다
            changed = any(before[key] != after[key] for key in before.keys() & after.keys())
        else:
            changed = before != after
        if changed:
            changes.append(Change(name, name, None, urgent=name in _FLOOR_FIELDS, human=False))
    return changes
