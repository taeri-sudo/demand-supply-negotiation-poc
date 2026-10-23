"""capacity_pools 증감을 위한 Lock 보호 헬퍼.

asyncio는 단일 프로세스·단일 스레드지만, get_field로 읽은 뒤 set_field로
쓰기까지 사이에 await 지점이 끼면 다른 태스크가 끼어들어 갱신이 유실될 수
있다(race condition). capacity_pools처럼 여러 태스크가 동시에 건드리는
공유자원은 asyncio.Lock으로 read-modify-write 구간을 감싼다
(STATE_SCHEMA.md "공유 자원 보호").
"""

from pathlib import Path

import pandas as pd

from .access import StateStore
from .data_source_judgment import last_complete_month
from .external_data import load_data_end, load_line_assignments, load_sales_orders, primary_line
from .ids import now_iso
from .state import Allocation, CapacityAdjustment, CapacityPool

# 라인의 `total_capacity` 초기값 = 그 라인 item들의 평소 월 수요 / 가동률. 가동률은 초기값을 계산할 때만 쓰고 그 뒤 값은 고정이다.
# 임시값이며 근거 확인이 필요하다(STATE_SCHEMA.md 2번 절).
CAPACITY_UTILIZATION = 0.8
NORMAL_DEMAND_MONTHS = 12  # 평소 월 수요를 보는 최근 완전한 달 수 (초기값)


async def adjust_capacity_pool(
    store: StateStore,
    role_tag: str,
    line_id: str,
    delta: float,
    reason: str,
) -> None:
    async with store.lock(f"capacity_pools:{line_id}"):
        pools = store.get_field(role_tag, "capacity_pools")
        index = next(i for i, pool in enumerate(pools) if pool.line_id == line_id)
        pool = pools[index]
        updated = pool.model_copy(
            update={
                "remaining_capacity": pool.remaining_capacity + delta,
                "adjustment_history": [
                    *pool.adjustment_history,
                    CapacityAdjustment(ts=now_iso(), delta=delta, reason=reason),
                ],
            }
        )
        store.set_field(
            role_tag, f"capacity_pools[{index}]", updated
        )


def initial_allocations(pools: list[CapacityPool]) -> list[Allocation]:
    """시스템을 처음 설정할 때 라인마다 만드는 빈 `allocations` 기록. 첫 조율 전의 보류도 기록 상태로 신호가 가게 한다."""
    return [Allocation(line_id=pool.line_id) for pool in pools]


def normal_monthly_demand(orders: pd.DataFrame, data_end: pd.Timestamp) -> pd.Series:
    """(고객사, item)별 평소 월 수요 = 최근 `NORMAL_DEMAND_MONTHS`개 완전한 달의 월 주문량 평균(주문 없는 달은 0)."""
    last = last_complete_month(data_end)
    months = pd.date_range(last - pd.DateOffset(months=NORMAL_DEMAND_MONTHS - 1), last, freq="MS")
    month = orders["order_date"].dt.to_period("M").dt.to_timestamp()
    monthly = orders["quantity"].groupby([orders["company_id"], orders["item_id"], month.rename("month")]).sum()
    table = monthly.unstack("month").reindex(columns=months).fillna(0.0)  # 주문 없는 달은 0
    return table.mean(axis=1).rename("normal_monthly_demand")


def initial_capacity_pools(
    assignments: pd.DataFrame, demand: pd.Series, on: pd.Timestamp, utilization: float = CAPACITY_UTILIZATION
) -> list[CapacityPool]:
    """라인마다 풀 하나. `total_capacity` = 그 라인을 주 라인으로 쓰는 item들의 평소 월 수요 합계 / 가동률, `remaining_capacity`는 같은 값이다.

    `demand`는 `normal_monthly_demand`의 결과(고객사, item별)이다. 라인 소속은 주 라인(`preference: "primary"`)만 본다(M4).
    """
    by_item = demand.groupby("item_id").sum()
    totals: dict[str, float] = {line: 0.0 for line in sorted(set(assignments[assignments["preference"] == "primary"]["line_id"]))}
    for item in sorted(set(assignments["item_id"])):
        if (assignments[(assignments["item_id"] == item) & (assignments["preference"] == "primary")]).empty:
            continue
        totals[primary_line(assignments, item, on)] += float(by_item.get(item, 0.0))
    return [
        CapacityPool(line_id=line, total_capacity=total / utilization, remaining_capacity=total / utilization)
        for line, total in totals.items()
    ]


def load_initial_capacity_pools(data_dir: str | Path | None = None) -> list[CapacityPool]:
    """고정 샘플에서 시스템을 처음 설정할 때의 `capacity_pools`를 만든다(스냅샷 기준일의 라인 기준 정보와 주문 이력 기준)."""
    data_end = load_data_end(data_dir)
    return initial_capacity_pools(
        load_line_assignments(data_dir), normal_monthly_demand(load_sales_orders(data_dir), data_end), data_end
    )
