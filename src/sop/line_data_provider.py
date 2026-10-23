"""고정 샘플을 읽는 외부 데이터 제공자 (`coordination_inputs.LineDataProvider`의 구현, AGENT_NODE_LIST.md "외부 경계").

`SampleLineDataProvider`는 생성 시점에 샘플 파일을 한 번 읽어 스냅샷 기준일(`as_of`)의 외부 데이터를 고정하고, 라인 id를 받으면 그 라인의
`ExternalLineData`를 돌려준다. 실제 데이터나 API로 바꿀 때는 같은 호출 모양(`line_id -> ExternalLineData`)의 다른 구현으로 바꾼다.

- 라인 소속은 라인 기준 정보(`line_assignments`)의 주 라인(`primary`)이다(`line_of`). M4는 주 라인만 쓴다.
- 수주는 스냅샷 기준일까지 들어온 것 중 수주잔고가 남은 것만 넣는다. 납품을 마친 수주는 수요가 아니다.
- 최소 구매 약정의 `taken_quantity`(이미 가져간 양)는 약정 기간 안에 납기가 온 수주의 납품한 양(기준일까지)의 합이다.
- 계약 단위는 라인 item마다 상품별 기본값과, 그 item을 쓰는 고객사마다 고객사별 덮어쓰기를 반영한 값을 `unit_key`로 담는다.
- 1개당 마진은 item의 업종 원가(`cost_inputs`의 기준일 시점 값)의 공급가 - 제조원가다. item의 업종은 인스턴스 목록에서 찾고, 인스턴스가 없는
  item(신제품)은 같은 라인의 다른 item의 업종을 쓴다. 업종을 알 수 없으면 KeyError다(기본값으로 채우지 않는다).
- 묶임(`pegs`)은 샘플에 없어 비어 있다. 생산 중 물량이 묶인 수주는 규칙이 그 물량만큼 강한 묶임으로 다룬다.
- 사람이 정한 수량은 샘플 파일의 값에 `add_manual`로 더한 값이 이어진다(파일은 바꾸지 않는다). 더하는 쪽은 `manual_quantity_input.py`다.
"""

from datetime import date
from pathlib import Path

import pandas as pd

from .coordination_inputs import ExternalLineData
from .cost_inputs import CostSchedule, default_cost_schedule
from .external_data import (
    contract_unit,
    load_contract_units,
    load_instances,
    load_inventory,
    load_line_assignments,
    load_manual_quantities,
    load_min_commitments,
    load_sales_orders,
    load_supply_guarantees,
    load_work_in_progress,
    primary_line,
)
from .supply_allocation_rules import Commitment, Guarantee, ManualDemand, OrderDemand, WipLot, unit_key


class SampleLineDataProvider:
    """샘플 파일에서 읽은 스냅샷 기준일의 외부 데이터를 라인 단위로 돌려준다."""

    def __init__(self, as_of: date, data_dir: str | Path | None = None, cost_schedule: CostSchedule | None = None) -> None:
        self.as_of = as_of
        self._cost = cost_schedule if cost_schedule is not None else default_cost_schedule()
        stamp = pd.Timestamp(as_of)
        assignments = load_line_assignments(data_dir)
        self._line_by_item: dict[str, str] = {
            str(item): primary_line(assignments, str(item), stamp)
            for item in sorted(set(assignments[assignments["preference"] == "primary"]["item_id"]))
        }
        sales = load_sales_orders(data_dir)
        sales = sales[sales["order_date"] <= stamp]
        self._all_orders = sales
        self._open_orders = sales[sales["quantity"] > sales["delivered_quantity"]]
        manual = load_manual_quantities(data_dir)
        self._manual: list[ManualDemand] = [
            ManualDemand(
                demand_id=str(row.demand_id), company_id=None if pd.isna(row.company_id) else str(row.company_id),
                item_id=str(row.item_id), quantity=float(row.quantity),  # pyright: ignore[reportArgumentType]
            )
            for row in manual[manual["entered_at"] <= stamp].itertuples()
        ]
        self._commitments = load_min_commitments(data_dir)
        self._guarantees = load_supply_guarantees(data_dir)
        self._units = load_contract_units(data_dir)
        self._inventory = load_inventory(data_dir)
        self._wip = load_work_in_progress(data_dir)
        instances = load_instances(data_dir)
        self._pairs = sorted(zip(instances["company_id"], instances["item_id"]))
        self._industry_by_item = {str(item): str(industry) for item, industry in zip(instances["item_id"], instances["industry"])}

    # --- 라인 기준 정보 ---------------------------------------------------------------------------------------------------

    def lines(self) -> list[str]:
        return sorted(set(self._line_by_item.values()))

    def line_of(self, item_id: str) -> str:
        """item의 주 라인. 라인 기준 정보에 없으면 KeyError다."""
        try:
            return self._line_by_item[item_id]
        except KeyError:
            raise KeyError(f"주 라인이 없는 item: {item_id}") from None

    def unit(self, company_id: str | None, item_id: str) -> float:
        return contract_unit(self._units, company_id, item_id)

    # --- 사람이 정한 수량 ---------------------------------------------------------------------------------------------------

    def manual_quantities(self) -> list[ManualDemand]:
        return list(self._manual)

    def add_manual(self, demand: ManualDemand) -> None:
        self._manual.append(demand)

    # --- 외부 데이터 ------------------------------------------------------------------------------------------------------

    def __call__(self, line_id: str) -> ExternalLineData:
        items = sorted(item for item, line in self._line_by_item.items() if line == line_id)
        if not items:
            raise KeyError(f"주 라인이 {line_id}인 item이 없음")
        in_line = set(items)
        orders = [
            OrderDemand(
                demand_id=str(row.demand_id), company_id=str(row.company_id), item_id=str(row.item_id), quantity=float(row.quantity),  # pyright: ignore[reportArgumentType]
                due_date=row.due_date.date(), delivered_quantity=float(row.delivered_quantity),  # pyright: ignore[reportArgumentType, reportAttributeAccessIssue]
            )
            for row in self._open_orders[self._open_orders["item_id"].isin(in_line)].itertuples()
        ]
        manual = [m for m in self._manual if m.item_id in in_line]
        commitments = [
            Commitment(
                company_id=str(row.company_id), item_id=str(row.item_id), binding=bool(row.binding), total_quantity=float(row.total_quantity),  # pyright: ignore[reportArgumentType]
                period_start=row.period_start.date(), period_end=row.period_end.date(),  # pyright: ignore[reportAttributeAccessIssue]
                taken_quantity=self._taken_quantity(str(row.company_id), str(row.item_id), row.period_start, row.period_end),  # pyright: ignore[reportArgumentType]
            )
            for row in self._commitments[self._commitments["item_id"].isin(in_line)].itertuples()
        ]
        guarantees = [
            Guarantee(company_id=str(row.company_id), item_id=str(row.item_id), quantity=float(row.quantity))  # pyright: ignore[reportArgumentType]
            for row in self._guarantees[self._guarantees["item_id"].isin(in_line)].itertuples()
        ]
        wip = [
            WipLot(item_id=str(row.item_id), quantity=float(row.quantity), demand_ref=str(row.demand_ref))  # pyright: ignore[reportArgumentType]
            for row in self._wip[self._wip["item_id"].isin(in_line)].itertuples()
        ]
        stock = self._inventory[self._inventory["item_id"].isin(in_line)]["quantity"]
        return ExternalLineData(
            orders=orders, manual=manual, commitments=commitments, guarantees=guarantees, work_in_progress=wip,
            units=self._units_of(items, orders, manual, commitments, guarantees), margins={item: self._margin(item, line_id) for item in items},
            inventory=float(stock.sum()),
        )

    def _taken_quantity(self, company_id: str, item_id: str, period_start: pd.Timestamp, period_end: pd.Timestamp) -> float:
        """약정 기간 안에 납기가 온(기준일까지) 같은 (고객사, item)의 수주 중 납품한 양의 합."""
        orders = self._all_orders
        inside = orders[
            (orders["company_id"] == company_id) & (orders["item_id"] == item_id)
            & (orders["due_date"] >= period_start) & (orders["due_date"] <= min(period_end, pd.Timestamp(self.as_of)))
        ]
        return float(inside["delivered_quantity"].sum())

    def _units_of(self, items, orders, manual, commitments, guarantees) -> dict[str, float]:
        """라인 item의 상품별 기본값과, 그 item을 쓰는 (고객사, item)마다의 계약 단위."""
        pairs: set[tuple[str | None, str]] = {(None, item) for item in items}
        pairs |= {(company, item) for company, item in self._pairs if item in items}
        pairs |= {(o.company_id, o.item_id) for o in orders} | {(m.company_id, m.item_id) for m in manual}
        pairs |= {(c.company_id, c.item_id) for c in commitments} | {(g.company_id, g.item_id) for g in guarantees}
        return {unit_key(company, item): self.unit(company, item) for company, item in sorted(pairs, key=lambda p: (p[0] or "", p[1]))}

    def _margin(self, item_id: str, line_id: str) -> float:
        industry = self._industry_by_item.get(item_id)
        if industry is None:  # 인스턴스가 없는 item(신제품)은 같은 라인의 다른 item의 업종을 쓴다
            industry = next(
                (self._industry_by_item[other] for other, line in sorted(self._line_by_item.items()) if line == line_id and other in self._industry_by_item),
                None,
            )
        if industry is None:
            raise KeyError(f"업종을 알 수 없는 item: {item_id}")
        period = self._cost.at(industry, self.as_of)  # pyright: ignore[reportArgumentType]
        return period.supply_price - period.manufacturing_cost
