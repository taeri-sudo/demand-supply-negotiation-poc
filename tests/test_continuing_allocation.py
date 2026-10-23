"""이어지는 조율의 배분(고정한 항목), 사람이 넣은 하한 수량·강한 묶임 수량의 반영 (AGENT_NODE_LIST.md supply_coordination "이어지는 조율").

[테스트 전용 입력] 수요·capacity는 fixture 값이다(`supply_fixtures.py`). 계약 단위는 6, 1개당 마진은 모두 같다.
"""

from datetime import date

from sop.supply_allocation_rules import allocate_line, entries_of
from supply_fixtures import by_ref, commitment, forecast, inputs, order

EARLY_AUG = date(2017, 8, 5)  # 스냅샷 기준일(2017-08-15) 이전 → 미납분
LATE_AUG = date(2017, 8, 25)  # 이번 주기 안


def two_items(capacity, **kwargs):
    """I1(F1, A)과 I2(F2, A)의 예측 수요 300씩 + I1의 새 수주 O3(B) 120."""
    return inputs(
        forecasts=[forecast("F1", "A", "I1", 300), forecast("F2", "A", "I2", 300)],
        orders=[order("O3", "B", "I1", 120, LATE_AUG)], capacity=capacity, **kwargs,
    )


def test_a_continuing_allocation_keeps_the_untouched_items_and_shares_the_rest_for_the_touched_item():
    full = allocate_line(two_items(600))  # 처음부터 나누면 I2도 비율로 줄어든다
    assert by_ref(entries_of(full)) == {("F1", "soft"): 246, ("F2", "soft"): 246, ("O3", "soft"): 96}

    frozen = {("F2", "soft"): 300.0}  # 이전 결론에서 I2는 300이었다
    continuing = allocate_line(two_items(600), frozen=frozen)

    assert by_ref(entries_of(continuing)) == {("F1", "soft"): 210, ("F2", "soft"): 300, ("O3", "soft"): 84}
    assert continuing.judgment["frozen"] == [{"ref": "F2", "peg": "soft", "quantity": 300.0}]


def test_the_new_value_takes_the_remaining_supply_first_and_the_existing_entries_keep_their_quantity():
    result = allocate_line(two_items(1000), frozen={("F2", "soft"): 300.0})

    assert by_ref(entries_of(result)) == {("F1", "soft"): 300, ("F2", "soft"): 300, ("O3", "soft"): 120}  # 남은 공급 400 안에서 모두 채워진다
    assert result.judgment["unallocated"] == 280


def test_a_hard_peg_is_not_moved_and_only_the_soft_entries_of_the_touched_item_give_way():
    result = allocate_line(two_items(600, pegs={"F2": "hard"}), frozen={})  # I2는 강한 묶임 300

    assert by_ref(entries_of(result)) == {("F1", "soft"): 210, ("F2", "hard"): 300, ("O3", "soft"): 84}


def test_a_frozen_quantity_is_capped_by_the_request_that_got_smaller():
    result = allocate_line(
        inputs(forecasts=[forecast("F2", "A", "I2", 120)], capacity=600), frozen={("F2", "soft"): 300.0}
    )

    assert by_ref(entries_of(result)) == {("F2", "soft"): 120}


def test_frozen_entries_that_no_longer_fit_the_supply_are_released_and_all_soft_entries_share_again():
    result = allocate_line(two_items(240), frozen={("F2", "soft"): 300.0})  # 고정한 300이 공급 240을 넘는다

    assert result.judgment["frozen"] == []
    assert by_ref(entries_of(result)) == {("F1", "soft"): 96, ("F2", "soft"): 96, ("O3", "soft"): 36}  # 약한 묶임 전체를 비율로 다시 나눈다


def test_a_frozen_entry_stays_out_of_the_contract_floor_stage():
    # I2의 계약 하한이 있어도 고정한 항목은 하한 단계에 들어가지 않는다(이전 결론에서 이미 지켜진 수량이다)
    result = allocate_line(
        two_items(600, commitments=[commitment("A", "I2", 3000)]), frozen={("F2", "soft"): 300.0}
    )

    assert result.judgment["outcome"] == "allocated" and result.judgment["need_floors"] == 0
    assert by_ref(entries_of(result))[("F2", "soft")] == 300


# --- 사람이 넣은 하한 수량·강한 묶임 수량 ---------------------------------------------------------------------------------------


def exceeding(**kwargs):
    return inputs(
        forecasts=[forecast("F-L1", "A", "I1", 1200)], orders=[order("ORD-L1", "B", "I1", 600, EARLY_AUG)],
        commitments=[commitment("A", "I1", 3000)], capacity=1000, **kwargs,
    )


def test_a_floor_override_replaces_the_contract_floor_of_the_demands_company_and_item():
    assert allocate_line(exceeding()).judgment["outcome"] == "commitments_exceed_capacity"  # 미납분 600 + 하한 600 > 1000

    result = allocate_line(exceeding(floor_overrides={"F-L1": 300.0}))  # 사람이 A/I1의 하한을 300으로 정했다

    assert result.judgment["outcome"] == "allocated" and result.judgment["floors"] == {"A|I1": 300}
    assert by_ref(entries_of(result))[("ORD-L1", "soft")] == 600  # 미납분은 그대로 지킨다


def test_a_floor_override_for_an_unknown_demand_is_ignored():
    result = allocate_line(exceeding(floor_overrides={"UNKNOWN": 0.0}))

    assert result.judgment["outcome"] == "commitments_exceed_capacity"


def test_a_peg_limit_reduces_the_hard_quantity_and_the_rest_becomes_a_soft_entry():
    base = inputs(forecasts=[forecast("F", "A", "I1", 600), forecast("G", "B", "I2", 600)], capacity=400, pegs={"F": "hard"})
    assert allocate_line(base).judgment["outcome"] == "pegged_exceed_capacity"  # 강한 묶임 600이 공급 400을 넘는다

    limited = allocate_line(base.model_copy(update={"peg_limits": {"F": 240.0}}))

    assert limited.judgment["outcome"] == "allocated"
    quantities = by_ref(entries_of(limited))
    assert quantities[("F", "hard")] == 240  # 강한 묶임은 값까지만 유지한다
    assert quantities[("F", "soft")] + quantities[("G", "soft")] <= 160  # 나머지는 약한 묶임이라 남은 공급을 나눠 쓴다
