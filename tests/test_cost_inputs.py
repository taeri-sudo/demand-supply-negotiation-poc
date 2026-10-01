from datetime import date

import pytest

from sop.cost_inputs import (
    DEFAULT_COST_RATIO,
    STORAGE_RATE_PER_MONTH,
    CostPeriod,
    CostSchedule,
    default_cost_schedule,
    unit_overage_cost,
    unit_shortage_cost,
)


def test_default_cost_ratios_per_industry():
    schedule = default_cost_schedule()
    on = date(2016, 1, 1)
    assert schedule.at("food_processing", on).manufacturing_cost == pytest.approx(0.76)
    assert schedule.at("beverage_non_alcoholic", on).manufacturing_cost == pytest.approx(0.60)
    assert schedule.at("beverage_alcoholic", on).manufacturing_cost == pytest.approx(0.46)
    assert all(schedule.at(i, on).supply_price == 1.0 for i in DEFAULT_COST_RATIO)


def test_perishable_leftover_costs_the_whole_manufacturing_cost():
    """신선식품이 남으면 폐기로 과잉 비용이 제조원가 전체가 된다."""
    period = default_cost_schedule().at("food_processing", date(2016, 1, 1))
    assert unit_overage_cost(period, perishable=True) == pytest.approx(0.76)
    # 신선이 아니면 보관비(제조원가의 월 2.1%)만 든다
    assert unit_overage_cost(period, perishable=False) == pytest.approx(0.76 * STORAGE_RATE_PER_MONTH)
    assert unit_overage_cost(period, perishable=False) < unit_overage_cost(period, perishable=True)


def test_shortage_cost_is_margin_plus_penalty_and_changes_with_customer_penalty_rate():
    """부족 비용 = (공급가 - 제조원가) + 결품 위약금. 위약률은 고객사별로 다르다."""
    period = default_cost_schedule().at("food_processing", date(2016, 1, 1))
    low = unit_shortage_cost(period, shortfall_penalty_rate=0.02)
    high = unit_shortage_cost(period, shortfall_penalty_rate=0.05)
    assert low == pytest.approx((1.0 - 0.76) + 0.02 * 1.0)
    assert high == pytest.approx((1.0 - 0.76) + 0.05 * 1.0)
    assert high > low


def test_cost_schedule_uses_the_value_effective_for_the_planning_cycle():
    """적용 기간이 다른 원가 값을 넣으면 주기에 맞는 값을 쓴다."""
    schedule = CostSchedule(
        periods=[
            CostPeriod(industry="food_processing", effective_from=date(2013, 1, 1), supply_price=1.0, manufacturing_cost=0.70),
            CostPeriod(industry="food_processing", effective_from=date(2016, 7, 1), supply_price=1.0, manufacturing_cost=0.82),
            CostPeriod(industry="beverage_alcoholic", effective_from=date(2013, 1, 1), supply_price=1.0, manufacturing_cost=0.46),
        ]
    )
    assert schedule.at("food_processing", date(2016, 6, 30)).manufacturing_cost == 0.70
    assert schedule.at("food_processing", date(2016, 7, 1)).manufacturing_cost == 0.82
    assert schedule.at("food_processing", date(2017, 1, 1)).manufacturing_cost == 0.82
    # 값이 바뀌면 같은 수량의 부족 비용도 바뀐다(원가 상승으로 마진이 줄어듦)
    early = schedule.at("food_processing", date(2016, 6, 30))
    late = schedule.at("food_processing", date(2016, 7, 1))
    assert unit_shortage_cost(late, 0.03) < unit_shortage_cost(early, 0.03)


def test_cost_schedule_raises_when_no_period_is_effective_yet():
    schedule = default_cost_schedule()
    with pytest.raises(LookupError):
        schedule.at("food_processing", date(2012, 12, 31))
