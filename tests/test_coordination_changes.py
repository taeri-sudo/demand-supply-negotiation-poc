"""조율 입력의 변경: 마지막 조율의 입력과 지금 입력의 차이와 그 분류 (GRAPH_FLOW.md "조율 흐름" 이어지는 조율의 시점).

[테스트 전용 입력] 수요·capacity는 fixture 값이다(`supply_fixtures.py`). 스냅샷 기준일은 2017-08-15, 변경 동결 구간은 7일(2017-08-22까지)이다.
"""

from datetime import date

from sop.coordination_changes import Change, diff_inputs, immediate, touched_items
from supply_fixtures import commitment, forecast, guarantee, inputs, manual, order

INSIDE_FREEZE = date(2017, 8, 20)
OUTSIDE_FREEZE = date(2017, 8, 28)
OVERDUE = date(2017, 8, 5)


def base(**kwargs):
    return inputs(forecasts=[forecast("F1", "A", "I1", 300)], capacity=1000, **kwargs)


def one(changes: list[Change]) -> Change:
    assert len(changes) == 1, changes
    return changes[0]


def test_identical_inputs_have_no_change():
    assert diff_inputs(base(), base()) == []


def test_a_new_order_outside_the_freeze_window_is_an_ordinary_change_for_its_item_only():
    change = one(diff_inputs(base(), base(orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE)])))

    assert (change.kind, change.ref, change.items, change.urgent, change.human) == ("order", "O1", frozenset(["I2"]), False, False)
    assert not immediate([change])


def test_a_new_order_inside_the_freeze_window_or_overdue_is_urgent():
    for due in (INSIDE_FREEZE, OVERDUE):
        change = one(diff_inputs(base(), base(orders=[order("O1", "B", "I2", 60, due)])))
        assert change.urgent and not change.human and immediate([change]), due


def test_an_order_for_a_customer_with_a_contract_floor_is_urgent_even_outside_the_freeze_window():
    with_floor = base(guarantees=[guarantee("B", "I2", 12)])

    change = one(diff_inputs(with_floor, base(guarantees=[guarantee("B", "I2", 12)], orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE)])))

    assert change.urgent  # 계약 하한에 영향을 준다


def test_a_demand_change_that_leaves_the_applied_contract_floor_unchanged_is_not_urgent():
    # 하한 12는 이번 주기 수요(60, 72)보다 작아 수요가 바뀌어도 실제 적용되는 하한은 12 그대로다
    before = base(guarantees=[guarantee("B", "I2", 12)], orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE)])
    after = base(guarantees=[guarantee("B", "I2", 12)], orders=[order("O1", "B", "I2", 72, OUTSIDE_FREEZE)])

    change = one(diff_inputs(before, after))

    assert not change.urgent and not immediate([change])


def test_a_change_that_lowers_the_applied_floor_by_shrinking_the_demand_is_urgent():
    before = base(guarantees=[guarantee("B", "I2", 12)], orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE)])
    after = base(guarantees=[guarantee("B", "I2", 12)], orders=[order("O1", "B", "I2", 6, OUTSIDE_FREEZE)])  # 하한이 12에서 6으로 제한된다

    assert one(diff_inputs(before, after)).urgent


def test_a_forecast_change_above_the_applied_floor_is_not_urgent():
    with_commitment = [commitment("A", "I1", 600)]  # 이번 주기 몫 120, 예측 수요 300 → 360은 모두 하한(120)보다 크다

    change = one(diff_inputs(base(commitments=with_commitment), inputs(forecasts=[forecast("F1", "A", "I1", 360)], capacity=1000, commitments=with_commitment)))

    assert not change.urgent


def test_a_change_of_the_commitment_or_guarantee_itself_is_urgent():
    assert one(diff_inputs(base(guarantees=[guarantee("A", "I1", 12)]), base(guarantees=[guarantee("A", "I1", 18)]))).urgent


def test_a_manual_quantity_is_always_an_immediate_human_change():
    change = one(diff_inputs(base(), base(manual=[manual("M1", None, "I3", 120)])))

    assert (change.kind, change.items, change.urgent, change.human) == ("manual", frozenset(["I3"]), False, True)


def test_a_forecast_change_is_a_human_change_only_for_a_value_a_person_entered():
    changed = inputs(forecasts=[forecast("F1", "A", "I1", 360)], capacity=1000)

    ordinary = one(diff_inputs(base(), changed))
    human = one(diff_inputs(base(), changed, human_forecasts=frozenset(["F1"])))

    assert (ordinary.items, ordinary.urgent, ordinary.human) == (frozenset(["I1"]), False, False)
    assert human.human and immediate([human])


def test_a_forecast_for_a_customer_with_a_binding_commitment_is_urgent():
    before = base(commitments=[commitment("A", "I1", 3000)])
    after = inputs(forecasts=[forecast("F1", "A", "I1", 360)], capacity=1000, commitments=[commitment("A", "I1", 3000)])

    change = one(diff_inputs(before, after))

    assert change.urgent


def test_a_removed_demand_is_a_change_for_its_item():
    change = one(diff_inputs(base(orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE)]), base()))

    assert (change.kind, change.items) == ("order", frozenset(["I2"]))


def test_a_human_floor_or_peg_value_is_an_immediate_change_for_the_demands_item():
    floor = one(diff_inputs(base(), base(floor_overrides={"F1": 60.0})))
    peg = one(diff_inputs(base(), base(peg_limits={"F1": 60.0})))
    unknown = one(diff_inputs(base(), base(floor_overrides={"NOT-A-DEMAND": 60.0})))

    assert (floor.kind, floor.items, floor.urgent, floor.human) == ("floor_override", frozenset(["I1"]), False, True)
    assert (peg.kind, peg.items, peg.urgent, peg.human) == ("peg_limit", frozenset(["I1"]), False, True)
    assert immediate([floor]) and immediate([peg])  # 사람의 처리 결과는 급한 변경이 아니라 `human`으로 즉시 조율한다
    assert unknown.items is None  # 어느 item인지 알 수 없다


def test_a_supply_side_change_has_no_known_scope_and_touches_every_item():
    capacity = one(diff_inputs(base(), inputs(forecasts=[forecast("F1", "A", "I1", 300)], capacity=800)))
    commitments = one(diff_inputs(base(), base(commitments=[commitment("A", "I1", 600)])))

    assert (capacity.kind, capacity.items, capacity.urgent) == ("remaining_capacity", None, False)
    assert commitments.items is None and commitments.urgent  # 계약 하한에 영향을 준다
    assert touched_items([capacity]) is None


def test_the_touched_items_are_the_union_and_none_when_any_scope_is_unknown():
    two = diff_inputs(base(), base(orders=[order("O1", "B", "I2", 60, OUTSIDE_FREEZE), order("O2", "B", "I3", 60, OUTSIDE_FREEZE)]))

    assert touched_items(two) == frozenset(["I2", "I3"])
    assert touched_items([*two, Change("inventory", "inventory", None, False, False)]) is None
