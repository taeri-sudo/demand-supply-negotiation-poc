"""supply_coordination의 배분 규칙: 한 생산라인의 수요를 공급 가능량 안에서 나눈다 (AGENT_NODE_LIST.md supply_coordination agent).

State를 읽거나 쓰지 않는 순수 계산이다. 입력(`LineInputs`)은 조율을 시작할 때 고정한 값이고, 같은 입력이면 입력 목록의 순서와
상관없이 같은 배분이 나온다. 반환은 공통 규칙 2의 `StructuredJudgment`(판단값 + 근거)이며 M7에서 LLM이 같은 스키마로 이어받는다.

흐름:
1. 수요 합치기: 같은 (고객사, item)에 예측 수요와 이번 주기 납기의 수주가 함께 있으면 이번 주기 수요는 둘 중 큰 쪽이다 — 예측 수요에서
   그 (고객사, item)의 이번 주기 수주잔고를 뺀 나머지만 예측 수요 항목으로 남긴다(같은 수요를 두 번 세지 않는다). 미납분(납기가 지난
   수주잔고)은 이와 따로 센다. 사람이 정한 수량은 합치지 않는다.
2. 공급 가능량 = 재고 + 생산 중 물량 + 이번 주기 잔여 capacity.
3. 순서대로 적용하며 앞 단계에서 공급이 모자라면 그 단계 안에서 나눈다: 0 강한 묶임 → 1 미납분(납기가 오래 지난 순, 같으면 요청량
   비율) → 2 계약 하한 → 3 1개당 마진이 큰 순 → 4 같은 마진이면 요청량 비율 → 5 계약 단위로 내림(자투리는 배분하지 않는다).
4. 강한 묶임 합계가 공급 가능량을 넘으면 `pegged_exceed_capacity`, 미납분과 계약 하한의 합이 남은 공급을 넘으면
   `commitments_exceed_capacity`로 끝내고 배분하지 않는다(`commitments_exceed_capacity`가 `notice`로 설정돼 있으면 알리고 진행하며,
   미납분을 먼저 채운 뒤 남은 공급을 하한끼리 비율로 나눈다).

계약 하한은 (고객사, item)별로 (구속력 있는 최소 구매 약정의 이번 주기 몫)과 (공급 보장 물량) 중 큰 쪽이고, 그 (고객사, item)의 이번 주기
수요(강한 묶임 제외)를 넘지 않는다. 구속력 없는 약정은 하한이 아니다. 하한은 미납분과 따로 더해진다(미납분은 지난 주문을 마저 채우는 것이고
하한은 이번 주기 몫이다). 약정의 이번 주기 몫 = (약정 총량 - 이미 가져간 양) / 계약 기간의 남은 개월 수(이번 주기 달 포함).
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .judgment import StructuredJudgment
from .state import AllocationEntry, DemandKind, NoticeThreshold, Peg

_EPS = 1e-9
AllocationOutcome = Literal["allocated", "pegged_exceed_capacity", "commitments_exceed_capacity"]
ExceedMode = Literal["intervention", "notice"]


def unit_key(company_id: str | None, item_id: str) -> str:
    """`LineInputs.units`의 키. 고객사가 없는 수량은 `|item`이다."""
    return f"{company_id or ''}|{item_id}"


class ForecastDemand(BaseModel):
    """전달된 forecast 기록의 `scenario.value`(그 (고객사, item)의 이번 주기 수요)."""

    agent_id: str
    company_id: str
    item_id: str
    quantity: float


class OrderDemand(BaseModel):
    """수주 한 건. 수주잔고 = 수량 - 납품한 양, 납기(`due_date`)가 기준일 이전이면 미납분이다."""

    demand_id: str
    company_id: str
    item_id: str
    quantity: float
    due_date: date
    delivered_quantity: float = 0.0

    @property
    def backlog(self) -> float:
        return self.quantity - self.delivered_quantity


class ManualDemand(BaseModel):
    """사람이 정한 수량(주문이 없는 수량). 고객사가 정해지지 않았을 수 있다."""

    demand_id: str
    company_id: str | None = None
    item_id: str
    quantity: float


class Commitment(BaseModel):
    """최소 구매 약정. `taken_quantity`는 이미 가져간 양(기간 안에 납품한 양)이다."""

    company_id: str
    item_id: str
    binding: bool
    total_quantity: float
    period_start: date
    period_end: date
    taken_quantity: float = 0.0


class Guarantee(BaseModel):
    """공급 보장 물량(계획 주기당)."""

    company_id: str
    item_id: str
    quantity: float


class WipLot(BaseModel):
    """생산 중 물량: 그 물량이 묶인 수주(`demand_ref`)에 강한 묶임으로 먼저 배정한다."""

    item_id: str
    quantity: float
    demand_ref: str


class LineInputs(BaseModel):
    """한 라인의 조율 한 번에 들어가는 입력. 조율을 시작할 때 고정하고 그 값으로만 끝까지 계산한다.

    `units`는 (고객사, item)별 계약 단위(`unit_key`), `margins`는 item별 1개당 마진이다. `pegs`는 강한 묶임으로 지정한 수요의
    `demand_ref`(M4는 테스트에서 지정한다). 생산 중 물량이 묶인 수주는 지정하지 않아도 그 물량만큼 강한 묶임이다.
    """

    line_id: str
    cycle: str  # 계획 주기 "YYYY-MM"(예측 대상 달)
    as_of: date  # 스냅샷 기준일. 납기가 기준일 이전인 수주잔고가 미납분이다
    cycle_end: date  # 이번 주기의 마지막 날. 납기가 기준일 뒤 ~ 이 날짜인 수주가 이번 주기 수주다
    forecasts: list[ForecastDemand] = Field(default_factory=list)
    orders: list[OrderDemand] = Field(default_factory=list)
    manual: list[ManualDemand] = Field(default_factory=list)
    commitments: list[Commitment] = Field(default_factory=list)
    guarantees: list[Guarantee] = Field(default_factory=list)
    work_in_progress: list[WipLot] = Field(default_factory=list)
    units: dict[str, float] = Field(default_factory=dict)
    margins: dict[str, float] = Field(default_factory=dict)
    inventory: float = 0.0  # 이 라인 item들의 재고 합계
    remaining_capacity: float = 0.0
    pegs: dict[str, Peg] = Field(default_factory=dict)

    def supply(self) -> float:
        """공급 가능량 = 재고 + 생산 중 물량 + 이번 주기 잔여 capacity."""
        return self.inventory + sum(lot.quantity for lot in self.work_in_progress) + self.remaining_capacity

    def unit(self, company_id: str | None, item_id: str) -> float:
        unit = self.units.get(unit_key(company_id, item_id), self.units.get(unit_key(None, item_id)))
        if unit is None:
            raise ValueError(f"계약 단위가 없음: {company_id}/{item_id}")
        return unit

    def cycle_start(self) -> date:
        return date(int(self.cycle[:4]), int(self.cycle[5:7]), 1)


def floor_to_unit(quantity: float, unit: float) -> float:
    return math.floor(round(quantity / unit, 9)) * unit


@dataclass
class _Demand:
    """배분 한 번 안의 수요 항목. `requested`는 계약 단위로 내린 요청량이다."""

    ref: str
    kind: DemandKind
    company_id: str | None
    item_id: str
    requested: float
    due_date: date | None
    peg: Peg
    overdue: bool
    unit: float
    allocated: float = 0.0

    def order_key(self) -> tuple:
        kind_rank = {"order": 0, "manual": 1, "forecast": 2}[self.kind]
        return (self.due_date or date.max, kind_rank, self.ref, self.peg)

    def remaining(self) -> float:
        return self.requested - self.allocated


def months_remaining(commitment: Commitment, cycle_start: date) -> int:
    """이번 주기 달을 포함해 계약 기간이 끝나는 달까지 남은 개월 수. 기간이 이미 끝났으면 0."""
    if commitment.period_end < cycle_start:
        return 0
    return (commitment.period_end.year - cycle_start.year) * 12 + commitment.period_end.month - cycle_start.month + 1


def commitment_share(commitment: Commitment, cycle_start: date, cycle_end: date) -> float:
    """약정의 이번 주기 몫 = (약정 총량 - 이미 가져간 양) / 남은 개월 수. 기간이 이번 주기와 겹치지 않거나 이미 채웠으면 0."""
    months = months_remaining(commitment, cycle_start)
    if months == 0 or commitment.period_start > cycle_end:
        return 0.0
    return max(0.0, commitment.total_quantity - commitment.taken_quantity) / months


def build_demands(inputs: LineInputs) -> list[_Demand]:
    """수요 합치기와 강한 묶임 구분. 같은 입력이면 같은 순서의 목록이 나온다."""
    wip_by_ref: dict[str, float] = {}
    for lot in inputs.work_in_progress:
        wip_by_ref[lot.demand_ref] = wip_by_ref.get(lot.demand_ref, 0.0) + lot.quantity
    demands: list[_Demand] = []

    def add_order(order: OrderDemand, overdue: bool) -> None:
        unit = inputs.unit(order.company_id, order.item_id)
        backlog = floor_to_unit(order.backlog, unit)
        if backlog <= 0:
            return
        hard_all = inputs.pegs.get(order.demand_id) == "hard"
        in_production = floor_to_unit(min(wip_by_ref.get(order.demand_id, 0.0), backlog), unit)
        if hard_all:
            in_production = backlog
        if in_production > 0:
            demands.append(_Demand(order.demand_id, "order", order.company_id, order.item_id, in_production, order.due_date, "hard", overdue, unit))
        if backlog - in_production > 0:
            demands.append(
                _Demand(order.demand_id, "order", order.company_id, order.item_id, backlog - in_production, order.due_date, "soft", overdue, unit)
            )

    cycle_backlog: dict[tuple[str, str], float] = {}
    for order in sorted(inputs.orders, key=lambda o: (o.due_date, o.demand_id)):
        if order.backlog <= 0:
            continue
        if order.due_date <= inputs.as_of:
            add_order(order, overdue=True)
        elif order.due_date <= inputs.cycle_end:
            add_order(order, overdue=False)
            key = (order.company_id, order.item_id)
            cycle_backlog[key] = cycle_backlog.get(key, 0.0) + order.backlog

    for forecast in sorted(inputs.forecasts, key=lambda f: f.agent_id):
        unit = inputs.unit(forecast.company_id, forecast.item_id)
        remainder = floor_to_unit(max(0.0, forecast.quantity - cycle_backlog.get((forecast.company_id, forecast.item_id), 0.0)), unit)
        if remainder > 0:
            peg: Peg = "hard" if inputs.pegs.get(forecast.agent_id) == "hard" else "soft"
            demands.append(_Demand(forecast.agent_id, "forecast", forecast.company_id, forecast.item_id, remainder, None, peg, False, unit))

    for manual in sorted(inputs.manual, key=lambda m: m.demand_id):
        unit = inputs.unit(manual.company_id, manual.item_id)
        quantity = floor_to_unit(manual.quantity, unit)
        if quantity > 0:
            peg = "hard" if inputs.pegs.get(manual.demand_id) == "hard" else "soft"
            demands.append(_Demand(manual.demand_id, "manual", manual.company_id, manual.item_id, quantity, None, peg, False, unit))
    return demands


def _share_out(demands: list[_Demand], amount: float) -> None:
    """`amount`를 요청량 비율로 나눈다(계약 단위로 내림). 요청량 합계가 `amount` 이하이면 모두 채운다."""
    total = sum(d.remaining() for d in demands)
    if total <= 0 or amount <= 0:
        return
    if amount + _EPS >= total:
        for d in demands:
            d.allocated += d.remaining()
        return
    for d in demands:
        d.allocated += min(d.remaining(), floor_to_unit(amount * d.remaining() / total, d.unit))


def _used(demands: list[_Demand]) -> float:
    return sum(d.allocated for d in demands)


def contract_floors(inputs: LineInputs, soft_cycle: list[_Demand]) -> dict[tuple[str, str], float]:
    """(고객사, item)별 계약 하한 = max(구속력 있는 약정의 이번 주기 몫, 공급 보장 물량)을 계약 단위로 내리고 그 이번 주기 수요를 넘지 않게 한다."""
    cycle_start = inputs.cycle_start()
    wanted: dict[tuple[str, str], float] = {}
    for commitment in inputs.commitments:
        if commitment.binding:
            key = (commitment.company_id, commitment.item_id)
            wanted[key] = max(wanted.get(key, 0.0), commitment_share(commitment, cycle_start, inputs.cycle_end))
    for guarantee in inputs.guarantees:
        key = (guarantee.company_id, guarantee.item_id)
        wanted[key] = max(wanted.get(key, 0.0), guarantee.quantity)
    floors: dict[tuple[str, str], float] = {}
    for key, quantity in sorted(wanted.items()):
        demand = sum(d.requested for d in soft_cycle if (d.company_id, d.item_id) == key)
        amount = min(floor_to_unit(quantity, inputs.unit(*key)), demand)
        if amount > 0:
            floors[key] = amount
    return floors


def commitment_gaps(inputs: LineInputs, entries: list[AllocationEntry], threshold: NoticeThreshold) -> list[dict]:
    """배분이 약정의 이번 주기 몫과 알림 기준 이상 차이 나는 약정. 차이 = |배분 - 몫| / 몫이고 기준은 약정의 구속력에 따라 다르다.

    비교하는 배분은 그 (고객사, item)의 이번 주기 항목(미납분 제외)의 합이다.
    """
    cycle_start = inputs.cycle_start()
    gaps = []
    for commitment in sorted(inputs.commitments, key=lambda c: (c.company_id, c.item_id)):
        share = commitment_share(commitment, cycle_start, inputs.cycle_end)
        if share <= 0:
            continue
        allocated = sum(
            e.quantity for e in entries
            if (e.company_id, e.item_id) == (commitment.company_id, commitment.item_id)
            and not (e.demand_kind == "order" and e.due_date is not None and e.due_date <= inputs.as_of)
        )
        ratio = abs(allocated - share) / share
        limit = threshold.binding if commitment.binding else threshold.non_binding
        if ratio + _EPS >= limit:
            gaps.append(
                {
                    "company_id": commitment.company_id, "item_id": commitment.item_id, "binding": commitment.binding,
                    "share": share, "allocated": allocated, "gap_ratio": ratio, "threshold": limit,
                }
            )
    return gaps


def _entry(d: _Demand) -> AllocationEntry:
    return AllocationEntry(
        demand_ref=d.ref, demand_kind=d.kind, company_id=d.company_id, item_id=d.item_id, quantity=d.allocated,
        due_date=d.due_date if d.kind == "order" else None, peg=d.peg,
    )


def allocate_line(inputs: LineInputs, exceed_mode: ExceedMode = "intervention") -> StructuredJudgment:
    """한 라인의 배분. `judgment["outcome"]`가 `allocated`면 `entries`(배분된 항목)가 있고, 아니면 배분하지 않고 escalation 사유만 있다."""
    supply = inputs.supply()
    demands = build_demands(inputs)
    hard = [d for d in demands if d.peg == "hard"]
    soft = [d for d in demands if d.peg == "soft"]
    for d in hard:
        d.allocated = d.requested
    hard_total = _used(hard)
    base = {"line_id": inputs.line_id, "cycle": inputs.cycle, "supply": supply, "hard_total": hard_total}
    if hard_total > supply + _EPS:
        return StructuredJudgment(
            judgment={**base, "outcome": "pegged_exceed_capacity", "entries": []},
            reasoning=f"강한 묶임 물량 합계 {hard_total:g}이 공급 가능량 {supply:g}을 넘음",
        )
    available = supply - hard_total

    overdue = sorted([d for d in soft if d.overdue], key=lambda d: d.order_key())
    cycle = [d for d in soft if not d.overdue]
    floors = contract_floors(inputs, cycle)
    need_overdue = sum(d.requested for d in overdue)
    need_floors = sum(floors.values())
    stage_detail = {"need_overdue": need_overdue, "need_floors": need_floors}
    exceeded = need_overdue + need_floors > available + _EPS
    if exceeded and exceed_mode == "intervention":
        return StructuredJudgment(
            judgment={**base, **stage_detail, "available_after_hard": available, "outcome": "commitments_exceed_capacity", "entries": []},
            reasoning=f"미납분 {need_overdue:g}과 계약 하한 {need_floors:g}의 합이 남은 공급 {available:g}을 넘음",
        )

    def left() -> float:
        return supply - sum(d.allocated for d in demands)

    # 1. 미납분: 납기가 오래 지난 순, 같은 납기끼리는 요청량 비율. 모자라면 그 납기에서 비율로 나누고 더 늦은 납기에는 배분하지 않는다
    for due in sorted({d.due_date for d in overdue}, key=lambda x: x or date.max):
        group = [d for d in overdue if d.due_date == due]
        _share_out(group, left())
        if any(d.remaining() > 0 for d in group):
            break

    # 2. 계약 하한: (고객사, item)의 이번 주기 수요를 납기 순, 사람이 정한 수량, 예측 수요 순으로 채운다. 남은 공급이 하한 합계보다
    #    모자라면(notice 모드) 하한끼리 비율로 나눈다
    floor_scale = 1.0 if need_floors <= left() + _EPS else left() / need_floors
    for key, floor in floors.items():
        target = floor if floor_scale >= 1.0 else floor_to_unit(floor * floor_scale, inputs.unit(*key))
        for d in sorted([d for d in cycle if (d.company_id, d.item_id) == key], key=lambda d: d.order_key()):
            take = min(d.remaining(), target)
            d.allocated += take
            target -= take

    # 3-4. 1개당 마진이 큰 순, 같은 마진이면 요청량 비율. 모자라면 그 마진 집단에서 비율로 나누고 더 낮은 마진에는 배분하지 않는다
    margins = {}
    for d in cycle:
        if d.item_id not in inputs.margins:
            raise ValueError(f"1개당 마진이 없음: {d.item_id}")
        margins[d.ref, d.peg] = round(inputs.margins[d.item_id], 9)
    for margin in sorted(set(margins.values()), reverse=True):
        group = [d for d in cycle if margins[d.ref, d.peg] == margin and d.remaining() > 0]
        _share_out(group, left())
        if any(d.remaining() > 0 for d in group):
            break

    allocated_demands = sorted([d for d in demands if d.allocated > 0], key=lambda d: (d.ref, d.peg, d.due_date or date.max))
    entries = [_entry(d) for d in allocated_demands]
    total_allocated = sum(e.quantity for e in entries)
    reasoning = (
        f"공급 가능량 {supply:g}(강한 묶임 {hard_total:g}) 중 {total_allocated:g}을 배분: 미납분 {_used(overdue):g}, "
        f"계약 하한 {sum(floors.values()) * min(floor_scale, 1.0):g}"
    ) + (", 하한 초과를 알리고 진행(notice)" if exceeded else "")
    return StructuredJudgment(
        judgment={
            **base, **stage_detail, "outcome": "allocated", "exceeded_notice": exceeded,
            "entries": [e.model_dump(mode="json") for e in entries],
            "unallocated": supply - total_allocated, "floors": {f"{c}|{i}": q for (c, i), q in floors.items()},
            "demands": [
                {"ref": d.ref, "kind": d.kind, "company_id": d.company_id, "item_id": d.item_id, "requested": d.requested,
                 "allocated": d.allocated, "peg": d.peg, "overdue": d.overdue}
                for d in sorted(demands, key=lambda d: (d.ref, d.peg))
            ],
        },
        reasoning=reasoning,
    )


def entries_of(judgment: StructuredJudgment) -> list[AllocationEntry]:
    """`allocate_line`이 낸 배분 항목."""
    return [AllocationEntry.model_validate(e) for e in judgment.judgment["entries"]]
