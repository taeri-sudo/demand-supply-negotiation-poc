"""supply_coordination 배분 규칙(AGENT_NODE_LIST.md): 수요 합치기, 공급 가능량, 미납분 -> 계약 하한 -> 마진 -> 비율 -> 계약 단위.

[테스트 전용 입력] 수요·capacity는 fixture 값이다. 기본 계약 단위는 6, 이번 주기는 2017-08(기준일 8월 15일)이다.
"""

from datetime import date
from typing import Any

import pytest

from sop.state import NoticeThreshold
from sop.supply_allocation_rules import (
    allocate_line,
    commitment_gaps,
    commitment_share,
    entries_of,
    months_remaining,
    unit_key,
)
from supply_fixtures import (
    by_ref,
    commitment,
    forecast,
    guarantee,
    inputs,
    manual,
    order,
    wip_lot,
)

LATE_JULY, EARLY_AUG, MID_AUG, END_AUG = date(2017, 7, 20), date(2017, 8, 5), date(2017, 8, 14), date(2017, 8, 25)


def run(**kwargs):
    mode = kwargs.pop("mode", "intervention")
    judgment = allocate_line(inputs(**kwargs), mode)
    return judgment, by_ref(entries_of(judgment)) if judgment.judgment["outcome"] == "allocated" else {}


# --- 수요 합치기 ---------------------------------------------------------------------------------------------------


def test_the_demand_is_the_larger_of_the_forecast_and_the_orders_due_this_cycle():
    _, smaller_forecast = run(forecasts=[forecast("F-A", "A", "I1", 300)], orders=[order("ORD-1", "A", "I1", 600, END_AUG)], capacity=10000)
    _, larger_forecast = run(forecasts=[forecast("F-A", "A", "I1", 1200)], orders=[order("ORD-1", "A", "I1", 600, END_AUG)], capacity=10000)

    assert smaller_forecast == {("ORD-1", "soft"): 600}  # 예측이 수주보다 작으면 수주가 수요다
    assert larger_forecast == {("ORD-1", "soft"): 600, ("F-A", "soft"): 600}  # 수주가 들어온 만큼 예측을 깎는다
    assert sum(larger_forecast.values()) == 1200  # 같은 수요를 두 번 세지 않는다


def test_forecast_and_orders_of_different_customers_or_items_are_not_merged():
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 300), forecast("F-A2", "A", "I2", 300)],
        orders=[order("ORD-B", "B", "I1", 600, END_AUG)], capacity=10000,
    )

    assert entries == {("F-A", "soft"): 300, ("F-A2", "soft"): 300, ("ORD-B", "soft"): 600}


def test_overdue_backlog_is_counted_separately_and_does_not_reduce_the_forecast():
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 1200)],
        orders=[order("ORD-LATE", "A", "I1", 300, EARLY_AUG, delivered=120)], capacity=10000,
    )

    assert entries == {("ORD-LATE", "soft"): 180, ("F-A", "soft"): 1200}  # 수주잔고 180이 미납분이다


def test_fully_delivered_orders_and_orders_due_after_this_cycle_are_not_demand():
    _, entries = run(
        orders=[
            order("ORD-DONE", "A", "I1", 300, EARLY_AUG, delivered=300),
            order("ORD-NEXT", "A", "I1", 300, date(2017, 9, 3)),
        ],
        capacity=10000,
    )

    assert entries == {}


def test_manual_quantities_are_demand_and_may_have_no_customer():
    _, entries = run(manual=[manual("MAN-1", None, "I1", 120)], capacity=10000)

    assert entries == {("MAN-1", "soft"): 120}
    assert entries_of(allocate_line(inputs(manual=[manual("MAN-1", None, "I1", 120)], capacity=10000)))[0].company_id is None


# --- 공급 가능량 ---------------------------------------------------------------------------------------------------


def test_the_supply_is_stock_plus_work_in_progress_plus_remaining_capacity():
    supply = inputs(inventory=100, wip=[wip_lot("I1", 50, "ORD-X")], capacity=200).supply()

    assert supply == 350


@pytest.mark.parametrize("inventory,wip,capacity", [(600, 0, 0), (0, 600, 0), (0, 0, 600), (200, 200, 200)])
def test_every_supply_source_counts_toward_what_can_be_allocated(inventory, wip, capacity):
    lots = [wip_lot("I1", wip, "ORD-NOT-OPEN")] if wip else []
    _, entries = run(forecasts=[forecast("F-A", "A", "I1", 600)], inventory=inventory, wip=lots, capacity=capacity)

    assert entries == {("F-A", "soft"): 600}


# --- 강한 묶임 -----------------------------------------------------------------------------------------------------


def test_work_in_progress_is_pegged_hard_to_its_order_and_the_rest_of_the_order_stays_soft():
    _, entries = run(orders=[order("ORD-W", "A", "I1", 300, END_AUG)], wip=[wip_lot("I1", 120, "ORD-W")], capacity=500)

    assert entries == {("ORD-W", "hard"): 120, ("ORD-W", "soft"): 180}


def test_pegged_quantity_is_allocated_first_even_when_supply_is_short():
    _, entries = run(
        forecasts=[forecast("F-B", "B", "I1", 1200)], orders=[order("ORD-W", "A", "I1", 300, END_AUG)],
        wip=[wip_lot("I1", 120, "ORD-W")], capacity=0,
    )

    assert entries == {("ORD-W", "hard"): 120}  # 공급은 생산 중 물량 120뿐이고 그것이 묶인 수주에 먼저 간다


def test_a_demand_pegged_by_the_test_is_fixed_before_anything_else():
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 396), forecast("F-B", "B", "I1", 1200)], pegs={"F-A": "hard"}, capacity=1200,
    )

    assert entries == {("F-A", "hard"): 396, ("F-B", "soft"): 804}


def test_hard_pegged_quantity_over_the_supply_is_pegged_exceed_capacity_and_allocates_nothing():
    judgment, entries = run(forecasts=[forecast("F-A", "A", "I1", 1200)], pegs={"F-A": "hard"}, capacity=500)

    assert judgment.judgment["outcome"] == "pegged_exceed_capacity" and entries == {}
    assert (judgment.judgment["hard_total"], judgment.judgment["supply"]) == (1200, 500)


# --- 미납분 -> 계약 하한 -> 마진 -> 비율 -> 계약 단위 ----------------------------------------------------------------------


def test_overdue_backlog_comes_before_the_forecast_when_supply_is_short():
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 1200)], orders=[order("ORD-LATE", "B", "I1", 300, EARLY_AUG)], capacity=600,
    )

    assert entries == {("ORD-LATE", "soft"): 300, ("F-A", "soft"): 300}


def test_the_oldest_overdue_order_is_filled_first_and_orders_due_the_same_day_share_by_request_ratio():
    _, oldest_first = run(
        orders=[order("ORD-OLD", "A", "I1", 300, LATE_JULY), order("ORD-NEW", "B", "I1", 300, EARLY_AUG)], capacity=400, mode="notice",
    )
    _, same_day = run(
        orders=[order("ORD-1", "A", "I1", 600, EARLY_AUG), order("ORD-2", "B", "I1", 300, EARLY_AUG)], capacity=450, mode="notice",
    )

    assert oldest_first == {("ORD-OLD", "soft"): 300, ("ORD-NEW", "soft"): 96}  # 오래 지난 것부터. 나머지 100은 계약 단위 6으로 내림
    assert same_day == {("ORD-1", "soft"): 300, ("ORD-2", "soft"): 150}  # 같은 납기끼리는 요청량 비율(2:1)


def test_a_binding_commitment_share_is_a_floor_and_the_rest_is_shared_by_request_ratio_in_contract_units():
    # A의 약정 총량 1200은 남은 5개월에 걸쳐 이번 주기 몫 240이다
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)],
        commitments=[commitment("A", "I1", 1200)], capacity=1200,
    )

    # 하한 240 -> 남은 960을 남은 요청량(A 960, B 1200)의 비율로 나누고 6단위로 내림: A 426, B 533->528
    assert entries == {("F-A", "soft"): 666, ("F-B", "soft"): 528}


def test_a_supply_guarantee_is_a_floor_and_the_larger_of_the_two_floors_is_used():
    _, from_guarantee = run(
        forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)],
        commitments=[commitment("A", "I1", 600)], guarantees=[guarantee("A", "I1", 300)], capacity=600,
    )

    # 약정 몫 120 < 보장 300 이므로 하한 300. 남은 300을 A 900, B 1200의 비율로: A 128->126, B 171->168
    assert from_guarantee == {("F-A", "soft"): 426, ("F-B", "soft"): 168}


def test_the_floor_never_exceeds_the_demand_of_that_customer_and_item():
    _, entries = run(forecasts=[forecast("F-A", "A", "I1", 60)], commitments=[commitment("A", "I1", 5000)], capacity=10000)

    assert entries == {("F-A", "soft"): 60}  # 하한 1000이어도 요청량 60까지만


def test_a_non_binding_commitment_is_not_a_floor():
    _, binding = run(
        forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)],
        commitments=[commitment("A", "I1", 1200, binding=True)], capacity=1200,
    )
    _, non_binding = run(
        forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)],
        commitments=[commitment("A", "I1", 1200, binding=False)], capacity=1200,
    )

    assert binding[("F-A", "soft")] > non_binding[("F-A", "soft")]
    assert non_binding == {("F-A", "soft"): 600, ("F-B", "soft"): 600}  # 하한이 없으면 같은 비율


def test_the_commitment_share_uses_what_was_already_taken_and_the_remaining_months():
    c = commitment("A", "I1", 1200, taken=300)

    assert months_remaining(c, date(2017, 8, 1)) == 5
    assert commitment_share(c, date(2017, 8, 1), date(2017, 8, 31)) == 180  # (1200 - 300) / 5
    assert commitment_share(commitment("A", "I1", 1200, taken=1500), date(2017, 8, 1), date(2017, 8, 31)) == 0  # 이미 채웠다
    ended = commitment("A", "I1", 1200).model_copy(update={"period_end": date(2017, 7, 31)})
    assert months_remaining(ended, date(2017, 8, 1)) == 0 and commitment_share(ended, date(2017, 8, 1), date(2017, 8, 31)) == 0


def test_a_higher_margin_per_unit_is_allocated_first_when_supply_is_short():
    _, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 600), forecast("F-B", "B", "I2", 600)], capacity=700,
        margins={"I1": 0.4, "I2": 0.2},
    )

    assert entries == {("F-A", "soft"): 600, ("F-B", "soft"): 96}  # 마진이 큰 item을 먼저. 남은 100을 6단위로 내림


def test_equal_margin_demands_share_by_request_ratio_so_nobody_gets_zero():
    _, entries = run(forecasts=[forecast("F-A", "A", "I1", 600), forecast("F-B", "B", "I1", 300)], capacity=450)

    assert entries == {("F-A", "soft"): 300, ("F-B", "soft"): 150}


def test_quantities_are_rounded_down_to_the_contract_unit_and_the_remainder_is_not_allocated():
    judgment, entries = run(
        forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)], capacity=1000, units={unit_key("B", "I1"): 24.0},
    )

    assert entries[("F-B", "soft")] % 24 == 0 and entries[("F-A", "soft")] % 6 == 0
    assert sum(entries.values()) <= 1000 and judgment.judgment["unallocated"] == 1000 - sum(entries.values())


def test_when_nothing_is_short_everyone_gets_the_requested_quantity():
    _, entries = run(forecasts=[forecast("F-A", "A", "I1", 600), forecast("F-B", "B", "I1", 300)], capacity=10000)

    assert entries == {("F-A", "soft"): 600, ("F-B", "soft"): 300}


# --- 하한 초과 ------------------------------------------------------------------------------------------------------


def exceeding() -> dict[str, Any]:
    return dict(
        forecasts=[forecast("F-A", "A", "I1", 1200)], orders=[order("ORD-LATE", "B", "I1", 600, EARLY_AUG)],
        commitments=[commitment("A", "I1", 3000)], capacity=1000,
    )


def test_overdue_backlog_plus_contract_floors_over_the_supply_is_commitments_exceed_capacity():
    judgment, entries = run(**exceeding())

    assert judgment.judgment["outcome"] == "commitments_exceed_capacity" and entries == {}
    assert (judgment.judgment["need_overdue"], judgment.judgment["need_floors"], judgment.judgment["available_after_hard"]) == (600, 600, 1000)


def test_in_notice_mode_the_overdue_is_filled_first_and_the_floors_share_what_is_left():
    judgment, entries = run(**exceeding(), mode="notice")

    assert judgment.judgment["outcome"] == "allocated" and judgment.judgment["exceeded_notice"] is True
    assert entries == {("ORD-LATE", "soft"): 600, ("F-A", "soft"): 396}  # 남은 400을 하한 600의 비율로 줄여 6단위로 내림


def test_exactly_enough_supply_for_overdue_and_floors_is_not_an_excess():
    judgment, _ = run(**{**exceeding(), "capacity": 1200})

    assert judgment.judgment["outcome"] == "allocated" and judgment.judgment["exceeded_notice"] is False


# --- 약정 차이 ------------------------------------------------------------------------------------------------------


def test_the_gap_to_the_commitment_share_is_judged_by_the_binding_threshold():
    thresholds = NoticeThreshold(binding=0.2, non_binding=0.05)
    data = inputs(
        forecasts=[forecast("F-A", "A", "I1", 216), forecast("F-B", "B", "I1", 216)],
        commitments=[commitment("A", "I1", 1200, binding=True), commitment("B", "I1", 1200, binding=False)], capacity=10000,
    )

    gaps = commitment_gaps(data, entries_of(allocate_line(data)), thresholds)

    # 둘 다 몫 240에 배분 216(차이 10%)이다. 구속력 없는 약정만 기준(5%)을 넘는다
    assert [(g["company_id"], g["binding"], round(g["gap_ratio"], 2)) for g in gaps] == [("B", False, 0.1)]


def test_overdue_entries_are_not_counted_as_this_cycles_allocation_for_the_gap():
    data = inputs(
        orders=[order("ORD-LATE", "A", "I1", 240, EARLY_AUG)], commitments=[commitment("A", "I1", 1200, binding=False)], capacity=10000,
    )

    gaps = commitment_gaps(data, entries_of(allocate_line(data)), NoticeThreshold(binding=0.2, non_binding=0.05))

    assert [g["allocated"] for g in gaps] == [0.0]  # 미납분 240은 이번 주기 몫과 비교하지 않는다


# --- 같은 입력이면 같은 결과 --------------------------------------------------------------------------------------------


def test_the_result_does_not_depend_on_the_order_of_the_inputs():
    base: dict[str, Any] = dict(
        forecasts=[forecast("F-A", "A", "I1", 1000), forecast("F-B", "B", "I2", 700), forecast("F-C", "C", "I1", 300)],
        orders=[order("ORD-1", "A", "I1", 300, EARLY_AUG), order("ORD-2", "B", "I2", 120, END_AUG), order("ORD-3", "C", "I1", 60, MID_AUG)],
        manual=[manual("MAN-1", None, "I2", 90)], commitments=[commitment("A", "I1", 1200), commitment("B", "I2", 600, binding=False)],
        guarantees=[guarantee("C", "I1", 60)], capacity=1500, margins={"I1": 0.3, "I2": 0.5},
    )
    reversed_inputs: dict[str, Any] = {key: list(reversed(value)) if isinstance(value, list) else value for key, value in base.items()}

    assert allocate_line(inputs(**base)) == allocate_line(inputs(**reversed_inputs))


def test_the_result_uses_the_common_judgment_schema_with_value_and_reasoning():
    from sop.judgment import StructuredJudgment

    judgment = allocate_line(inputs(forecasts=[forecast("F-A", "A", "I1", 60)], capacity=100))

    assert isinstance(judgment, StructuredJudgment) and judgment.reasoning and judgment.judgment["entries"]


def test_a_missing_margin_or_unit_is_an_error_not_a_silent_default():
    with pytest.raises(ValueError, match="마진"):
        allocate_line(inputs(forecasts=[forecast("F-A", "A", "I1", 60)], capacity=100, margins={}).model_copy(update={"margins": {}}))
    with pytest.raises(ValueError, match="계약 단위"):
        allocate_line(inputs(forecasts=[forecast("F-A", "A", "I1", 60)], capacity=100).model_copy(update={"units": {}}))
