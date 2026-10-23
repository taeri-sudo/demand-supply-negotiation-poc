"""라인 조율의 State 반영: 결론 쓰기, 하한 초과·강한 묶임 초과 escalation(이전 결론 유지), 약정 차이 알림, 배분 결론 검증과 실패 처리.

[테스트 전용 입력] 수요·capacity는 fixture 값이다(`supply_fixtures.py`).
"""

import pytest

from sop.access import PermissionDenied
from sop.allocation_validation import allocation_validation_rules
from sop.capacity import adjust_capacity_pool
from sop.state import Allocation, InteractionProtocol
from sop.supply_coordination_human_manager import commitments_exceed_capacity_protocol_entry
from sop.supply_coordination_line import check_allocation_verdict, run_line_coordination
from sop.validation_agent import process_pending_validations
from supply_fixtures import (
    SUPPLY,
    VALIDATOR,
    by_ref,
    commitment,
    drain,
    forecast,
    inputs,
    make_store,
    order,
    pending,
)
from datetime import date

pytestmark = pytest.mark.anyio

EARLY_AUG = date(2017, 8, 5)


def plain(line_id="L1", capacity=10000.0, **kwargs):
    return inputs(line_id=line_id, forecasts=[forecast(f"F-{line_id}", "A", "I1", 600)], capacity=capacity, **kwargs)


def exceeding(line_id="L1"):
    return inputs(
        line_id=line_id, forecasts=[forecast(f"F-{line_id}", "A", "I1", 1200)],
        orders=[order(f"ORD-{line_id}", "B", "I1", 600, EARLY_AUG)], commitments=[commitment("A", "I1", 3000)], capacity=1000,
    )


def allocation_of(store, index=0) -> Allocation:
    return store.state.allocations[index]


# --- 결론 쓰기 ------------------------------------------------------------------------------------------------------


def test_the_conclusion_overwrites_the_lines_record_unvalidated_and_signals_the_validator():
    store = make_store()
    drain(store)

    result = run_line_coordination(store, SUPPLY, plain())

    record = allocation_of(store)
    assert result.outcome == "allocated" and result.allocation == record
    assert (record.line_id, record.cycle, record.role_tag, record.validation) == ("L1", "2017-08", SUPPLY, None)
    assert by_ref(record.entries) == {("F-L1", "soft"): 600}
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"
    assert pending(store) == {VALIDATOR: 1}  # 같은 라인의 다른 기록이나 다른 라인에는 신호가 가지 않는다
    assert store.state.allocations[1].cycle is None  # 다른 라인은 그대로다


def test_the_decision_is_logged_with_the_previous_record_and_the_record_points_to_that_log_entry():
    store = make_store()
    run_line_coordination(store, SUPPLY, plain(capacity=10000))
    first_seq = allocation_of(store).log_seq

    run_line_coordination(store, SUPPLY, inputs(forecasts=[forecast("F-L1", "A", "I1", 300)], capacity=10000))

    decided = [e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_decided"]
    assert [e.seq for e in decided] == [first_seq, allocation_of(store).log_seq]
    assert decided[0].payload["previous"]["cycle"] is None  # 첫 조율의 덮어쓰기 전 값은 빈 기록이다
    assert by_ref(Allocation.model_validate(decided[1].payload["previous"]).entries) == {("F-L1", "soft"): 600}
    assert decided[1].payload["line_id"] == "L1" and decided[1].payload["judgment"]["supply"] == 10000
    assert decided[1].payload["inputs"]["forecasts"][0]["quantity"] == 300 and decided[1].agent_id is None


def test_each_line_is_coordinated_independently():
    store = make_store()

    run_line_coordination(store, SUPPLY, plain("L1"))
    run_line_coordination(store, SUPPLY, plain("L2", capacity=300))

    assert by_ref(allocation_of(store, 0).entries) == {("F-L1", "soft"): 600}
    assert by_ref(allocation_of(store, 1).entries) == {("F-L2", "soft"): 300}


def test_coordination_does_not_change_the_remaining_capacity():
    store = make_store(capacity=10000)

    run_line_coordination(store, SUPPLY, plain(capacity=10000))

    assert [p.remaining_capacity for p in store.state.capacity_pools] == [10000, 10000]
    with pytest.raises(PermissionDenied):
        store.set_field(SUPPLY, "capacity_pools[0].remaining_capacity", 0.0)  # supply_coordination은 capacity_pools를 읽기만 한다


# --- 하한 초과·강한 묶임 초과 -----------------------------------------------------------------------------------------------


def test_commitments_exceeding_the_supply_hold_the_line_keep_the_previous_conclusion_and_let_other_lines_go_on():
    store = make_store()
    run_line_coordination(store, SUPPLY, plain("L1"))
    before = allocation_of(store, 0)
    drain(store)

    held = run_line_coordination(store, SUPPLY, exceeding("L1"))
    other = run_line_coordination(store, SUPPLY, plain("L2"))

    assert held.outcome == "commitments_exceed_capacity" and held.allocation is None
    (escalation,) = [e for e in store.state.escalation_records if e.reason == "commitments_exceed_capacity"]
    assert (escalation.line_id, escalation.agent_id, escalation.target_role, escalation.mode, escalation.status) == (
        "L1", None, "human_manager", "intervention", "open",
    )
    assert escalation.trigger_edge == "supply_coordination->human_manager" and "계약 하한" in escalation.rationale
    assert allocation_of(store, 0) == before  # 이전 결론 유지
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_business"
    assert other.outcome == "allocated" and store.record_state(SUPPLY, "allocations[1]") == "awaiting_validation"
    entry = next(e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_escalated")
    assert escalation.log_seq == entry.seq and entry.payload["reason"] == "commitments_exceed_capacity"
    assert entry.payload["judgment"]["need_overdue"] == 600 and entry.payload["judgment"]["need_floors"] == 600


def test_a_hold_on_the_first_coordination_of_a_line_signals_the_human_manager():
    store = make_store()
    drain(store)

    result = run_line_coordination(store, SUPPLY, exceeding("L1"))

    assert result.outcome == "commitments_exceed_capacity"
    assert allocation_of(store, 0).cycle is None and allocation_of(store, 0).entries == []  # 첫 조율이라 이전 결론이 없다
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_business"
    assert pending(store) == {"human_manager": 1}  # 빈 기록이라도 기록 상태가 바뀌어 사람에게 신호가 간다
    assert store.queue("human_manager").get_nowait()["record_path"] == "allocations[0]"


def test_a_held_line_is_not_coordinated_again_until_it_is_handled():
    store = make_store()
    run_line_coordination(store, SUPPLY, exceeding("L1"))

    with pytest.raises(ValueError, match="처리되지 않은"):
        run_line_coordination(store, SUPPLY, plain("L1"))

    assert len(store.state.escalation_records) == 1


def test_hard_pegged_quantity_over_the_supply_holds_the_line_with_pegged_exceed_capacity():
    store = make_store()
    run_line_coordination(store, SUPPLY, plain("L1"))
    before = allocation_of(store, 0)

    result = run_line_coordination(
        store, SUPPLY, inputs(forecasts=[forecast("F-L1", "A", "I1", 1200)], pegs={"F-L1": "hard"}, capacity=300)
    )

    assert result.outcome == "pegged_exceed_capacity"
    (escalation,) = store.state.escalation_records
    assert (escalation.reason, escalation.line_id, escalation.target_role, escalation.mode) == (
        "pegged_exceed_capacity", "L1", "human_manager", "intervention",
    )
    assert allocation_of(store, 0) == before and store.record_state(SUPPLY, "allocations[0]") == "awaiting_business"


def test_changing_the_protocol_mode_to_notice_lets_the_line_go_on_without_code_changes():
    notice_entry = commitments_exceed_capacity_protocol_entry().model_copy(update={"escalation_mode": "notice"})
    store = make_store(protocol=[notice_entry])
    drain(store)

    result = run_line_coordination(store, SUPPLY, exceeding("L1"))

    assert result.outcome == "allocated" and allocation_of(store, 0).cycle == "2017-08"
    (notice,) = store.state.escalation_records
    assert (notice.reason, notice.mode, notice.status, notice.resolution, notice.line_id) == (
        "commitments_exceed_capacity", "notice", None, None, "L1",
    )
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"  # 멈추지 않는다
    assert pending(store) == {VALIDATOR: 1, "human_manager": 1}  # 사람에게는 알림 신호만 간다
    assert store.queue("human_manager").get_nowait()["record_path"] == "escalation_records[0]"


# --- 약정 차이 알림 ---------------------------------------------------------------------------------------------------


def gap_inputs():
    # 둘 다 이번 주기 몫 240에 배분 216(차이 10%). 구속력 있음은 기준 20% 미만, 없음은 기준 5% 이상
    return inputs(
        forecasts=[forecast("F-A", "A", "I1", 216), forecast("F-B", "B", "I1", 216)],
        commitments=[commitment("A", "I1", 1200, binding=True), commitment("B", "I1", 1200, binding=False)], capacity=10000,
    )


def test_only_the_non_binding_commitment_with_the_same_gap_sends_a_notice_and_the_line_goes_on():
    store = make_store()
    drain(store)

    result = run_line_coordination(store, SUPPLY, gap_inputs())

    (notice,) = store.state.escalation_records
    assert (notice.reason, notice.mode, notice.status, notice.target_role, notice.line_id) == (
        "commitment_gap", "notice", None, "human_manager", "L1",
    )
    assert "B/I1" in notice.rationale and "10%" in notice.rationale
    assert result.outcome == "allocated" and store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"  # 알림 후에도 멈추지 않는다
    entry = next(e for e in store.negotiation_log(SUPPLY) if e.event == "commitment_gap_noticed")
    assert notice.log_seq == entry.seq and entry.payload["gap"]["company_id"] == "B"
    assert pending(store) == {VALIDATOR: 1, "human_manager": 1}


def test_a_non_binding_commitment_is_not_used_as_a_floor():
    store = make_store()

    run_line_coordination(
        store, SUPPLY,
        inputs(
            forecasts=[forecast("F-A", "A", "I1", 1200), forecast("F-B", "B", "I1", 1200)],
            commitments=[commitment("A", "I1", 1200, binding=False)], capacity=1200,
        ),
    )

    assert by_ref(allocation_of(store).entries) == {("F-A", "soft"): 600, ("F-B", "soft"): 600}


def test_without_a_gap_entry_in_the_protocol_no_notice_is_made():
    store = make_store(protocol=[])

    run_line_coordination(store, SUPPLY, gap_inputs())

    assert store.state.escalation_records == [] and allocation_of(store).cycle == "2017-08"


# --- 배분 결론 검증과 실패 처리 -----------------------------------------------------------------------------------------


def validate(store, stock: float = 0.0):
    rules = {SUPPLY: allocation_validation_rules(store, VALIDATOR, lambda line_id: stock)}
    return process_pending_validations(store, VALIDATOR, rules)


def test_a_passed_conclusion_is_completed_and_signals_nobody():
    store = make_store()
    run_line_coordination(store, SUPPLY, plain())
    drain(store)

    store.queue(VALIDATOR).put_nowait({"field_path": "allocations[0]", "written_by": SUPPLY})
    (verdict,) = validate(store)

    assert verdict.status == "passed" and store.record_state(SUPPLY, "allocations[0]") == "completed"
    assert pending(store) == {}
    assert check_allocation_verdict(store, SUPPLY, "L1", plain()) == "completed"


def run_and_fail(store, stock_seen_by_validator: float, **kwargs):
    """재고를 쓰는 결론을 쓰고, 검증agent가 그 재고를 보지 못하는 것으로 두어 불합격을 만든다."""
    data = inputs(forecasts=[forecast("F-L1", "A", "I1", 600)], inventory=600, capacity=0, **kwargs)
    run_line_coordination(store, SUPPLY, data)
    (verdict,) = validate(store, stock=stock_seen_by_validator)
    assert verdict.status == "failed"
    return data


def test_a_failed_conclusion_with_unchanged_inputs_is_an_allocation_logic_error_for_the_system_operator():
    store = make_store(capacity=0)
    data = run_and_fail(store, stock_seen_by_validator=0.0)
    drain(store)

    action = check_allocation_verdict(store, SUPPLY, "L1", data)

    assert action == "invalid"
    verdict = allocation_of(store).validation
    assert verdict is not None and [c.type for c in verdict.suspected_causes] == ["allocation"]
    assert verdict.suspected_causes[0].issue == "exceeds_supply"
    (escalation,) = store.state.escalation_records
    assert (escalation.reason, escalation.target_role, escalation.mode, escalation.line_id) == (
        "allocation_invalid", "system_operator", "intervention", "L1",
    )
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_system" and pending(store) == {"system_operator": 1}


async def test_a_failed_conclusion_whose_inputs_changed_is_coordinated_again_without_an_escalation():
    store = make_store(capacity=0)
    run_and_fail(store, stock_seen_by_validator=0.0)
    await adjust_capacity_pool(store, "production_plan", "L1", 600, "라인 증설")  # 조율 뒤 capacity가 늘고 재고는 쓰지 않게 됐다
    changed = inputs(forecasts=[forecast("F-L1", "A", "I1", 600)], inventory=0, capacity=600)

    assert check_allocation_verdict(store, SUPPLY, "L1", changed) == "recoordinate"
    assert store.state.escalation_records == []
    run_line_coordination(store, SUPPLY, changed)  # 호출한 쪽이 바뀐 입력으로 다시 조율한다

    assert allocation_of(store).validation is None and store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"
    (verdict,) = validate(store, stock=0.0)
    assert verdict.status == "passed"


async def test_a_capacity_drop_after_the_conclusion_fails_validation_and_counts_as_changed_input():
    store = make_store(capacity=1000)
    run_line_coordination(store, SUPPLY, inputs(forecasts=[forecast("F-L1", "A", "I1", 600)], capacity=1000))

    await adjust_capacity_pool(store, "production_plan", "L1", -700, "라인 고장")  # 조율 뒤 capacity가 줄었다
    (verdict,) = validate(store)

    assert verdict.status == "failed" and store.state.capacity_pools[0].remaining_capacity == 300
    shrunk = inputs(forecasts=[forecast("F-L1", "A", "I1", 600)], capacity=300)
    assert check_allocation_verdict(store, SUPPLY, "L1", shrunk) == "recoordinate"


def test_a_conclusion_that_is_not_failed_needs_no_handling():
    store = make_store()
    run_line_coordination(store, SUPPLY, plain())

    assert check_allocation_verdict(store, SUPPLY, "L1", plain()) == "waiting"  # 아직 검증 대기


def test_unrelated_protocol_entries_are_ignored():
    other = InteractionProtocol(edge="x<->y", scope=["x", "y"])
    store = make_store(protocol=[other])

    result = run_line_coordination(store, SUPPLY, exceeding("L1"))

    assert result.outcome == "commitments_exceed_capacity"  # 항목이 없으면 기본 모드(intervention)다


def test_a_validation_error_on_a_line_is_cleared_by_the_operators_rerun_so_the_conclusion_is_validated_again():
    from sop.escalation_resolution import resolve_escalation

    store = make_store()
    run_line_coordination(store, SUPPLY, plain())

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    from sop.validation_agent import ValidationRules

    process_pending_validations(store, VALIDATOR, {SUPPLY: ValidationRules(lambda r: r.line_id, broken, instance_kind="line")})
    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_system"
    drain(store)

    resolve_escalation(store, "system_operator", 0, kind="rerun")

    assert allocation_of(store).validation is None and store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"
    assert pending(store) == {VALIDATOR: 1}


def test_the_operator_dismissing_an_allocation_invalid_hold_leaves_the_conclusion_as_it_was():
    from sop.escalation_resolution import resolve_escalation

    store = make_store(capacity=0)
    data = run_and_fail(store, stock_seen_by_validator=0.0)
    assert check_allocation_verdict(store, SUPPLY, "L1", data) == "invalid"
    before = allocation_of(store)

    resolve_escalation(store, "system_operator", 0, kind="dismiss")

    assert allocation_of(store) == before and store.state.escalation_records[0].status == "resolved"
    assert store.record_state(SUPPLY, "allocations[0]") == "judged"  # 보류가 풀려 다시 작성agent의 처리 상태다
