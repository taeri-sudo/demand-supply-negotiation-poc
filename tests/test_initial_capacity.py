"""라인의 `capacity_pools` 초기값(STATE_SCHEMA.md 2번 절): `total_capacity` = 그 라인 item들의 평소 월 수요 / 가동률(0.8, 임시값).

[테스트 전용 입력] 합성 주문으로 계산 규칙을 확인하고, 고정 샘플로 샘플 전체의 풀 구성을 확인한다(샘플이 없으면 건너뛴다).
"""

import pandas as pd
import pytest

from sop.capacity import (
    CAPACITY_UTILIZATION,
    NORMAL_DEMAND_MONTHS,
    initial_capacity_pools,
    load_initial_capacity_pools,
    normal_monthly_demand,
)
from sop.external_data import DEFAULT_DATA_DIR, load_line_assignments, load_sales_orders, primary_line

DATA_END = pd.Timestamp("2017-08-15")  # 마지막 완전한 달은 2017-07


def orders_of(company: str, item: str, monthly: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "company_id": company, "item_id": item, "order_date": pd.to_datetime([f"{m}-10" for m in monthly]),
            "quantity": list(monthly.values()), "is_first_order": False, "promotion": pd.array([False] * len(monthly), dtype="boolean"),
        }
    )


def test_the_utilization_is_the_provisional_0_8():
    assert CAPACITY_UTILIZATION == 0.8


def test_normal_demand_is_the_mean_of_the_last_complete_months_with_empty_months_as_zero():
    months = pd.date_range("2016-08-01", "2017-07-01", freq="MS")  # 마지막 12개 완전한 달
    assert len(months) == NORMAL_DEMAND_MONTHS
    orders = orders_of("C", "I", {m.strftime("%Y-%m"): 120.0 for m in months[:6]})  # 앞 6달만 주문

    demand = normal_monthly_demand(orders, DATA_END)

    assert demand[("C", "I")] == pytest.approx(60.0)  # 12달 평균(주문 없는 달은 0)


def test_normal_demand_ignores_months_before_the_window_and_the_incomplete_month():
    orders = pd.concat([
        orders_of("C", "I", {"2015-01": 9999.0, "2016-08": 12.0, "2017-07": 12.0, "2017-08": 9999.0}),
    ])

    assert normal_monthly_demand(orders, DATA_END)[("C", "I")] == pytest.approx(2.0)


def lines_frame(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"item_id": i, "line_id": line, "preference": pref, "valid_from": pd.Timestamp("2013-01-01")} for i, line, pref in rows]
    )


def demand_series(values: dict[tuple[str, str], float]) -> pd.Series:
    index = pd.MultiIndex.from_tuples(list(values), names=["company_id", "item_id"])
    return pd.Series(list(values.values()), index=index, dtype=float)


def test_a_line_capacity_is_the_sum_of_its_primary_items_demand_across_customers_divided_by_the_utilization():
    assignments = lines_frame([("A", "LINE-1", "primary"), ("B", "LINE-1", "primary"), ("C", "LINE-2", "primary")])
    demand = demand_series({("X", "A"): 80.0, ("Y", "A"): 40.0, ("X", "B"): 40.0, ("X", "C"): 16.0})

    pools = {p.line_id: p for p in initial_capacity_pools(assignments, demand, DATA_END)}

    assert pools["LINE-1"].total_capacity == pytest.approx((80 + 40 + 40) / 0.8)
    assert pools["LINE-2"].total_capacity == pytest.approx(16 / 0.8)
    assert all(p.remaining_capacity == p.total_capacity and p.adjustment_history == [] for p in pools.values())


def test_an_alternate_line_does_not_add_demand_in_m4():
    assignments = lines_frame([("A", "LINE-1", "primary"), ("A", "LINE-2", "alternate"), ("B", "LINE-2", "primary")])
    demand = demand_series({("X", "A"): 80.0, ("X", "B"): 8.0})

    pools = {p.line_id: p.total_capacity for p in initial_capacity_pools(assignments, demand, DATA_END)}

    assert pools == {"LINE-1": pytest.approx(100.0), "LINE-2": pytest.approx(10.0)}


def test_an_item_without_orders_adds_nothing_and_the_utilization_can_be_replaced():
    assignments = lines_frame([("NEW", "LINE-1", "primary"), ("A", "LINE-1", "primary")])
    demand = demand_series({("X", "A"): 50.0})

    (pool,) = initial_capacity_pools(assignments, demand, DATA_END, utilization=0.5)

    assert pool.total_capacity == pytest.approx(100.0)


@pytest.fixture(scope="module")
def sample_pools():
    if not (DEFAULT_DATA_DIR / "orders.csv").exists() or not (DEFAULT_DATA_DIR / "line_assignments.csv").exists():
        pytest.skip("data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다")
    return load_initial_capacity_pools()


def test_the_sample_has_one_pool_per_primary_line_with_positive_capacity(sample_pools):
    lines = load_line_assignments()
    expected = {primary_line(lines, item, DATA_END) for item in set(lines["item_id"])}
    assert {p.line_id for p in sample_pools} == expected
    assert all(p.total_capacity > 0 and p.remaining_capacity == p.total_capacity for p in sample_pools)


def test_the_sample_capacity_is_the_line_demand_over_the_utilization(sample_pools):
    lines = load_line_assignments()
    demand = normal_monthly_demand(load_sales_orders(), DATA_END).groupby("item_id").sum()
    for pool in sample_pools:
        items = [i for i in set(lines["item_id"]) if primary_line(lines, i, DATA_END) == pool.line_id]
        assert pool.total_capacity == pytest.approx(sum(float(demand.get(i, 0.0)) for i in items) / 0.8)
