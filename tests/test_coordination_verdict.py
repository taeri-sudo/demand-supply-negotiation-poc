"""배분 결론 검증 뒤의 처리 (AGENT_NODE_LIST.md supply_coordination "기록 쓰기와 검증"): 입력이 달라졌으면 다시 조율, 같으면 `allocation_invalid`,
그 `allocation_invalid`를 `system_operator`가 처리한 뒤의 `rerun`(그 라인을 다시 조율)과 `dismiss`(이번 주기에서 뺌).

[테스트 전용 입력] 수요·capacity는 fixture 값이다(`coordination_fixtures.py`). 입력이 같은데 불합격인 상황은 항상 불합격을 내는 검증 규칙으로 만든다.
"""

import pytest

from coordination_fixtures import (
    SUPPLY,
    SYSTEM,
    VALIDATOR,
    Data,
    coordinator,
    decided,
    deliver,
    entries_by_ref,
    line_state,
    make_store,
    pump,
    validate,
)
from sop.capacity import adjust_capacity_pool
from sop.escalation_resolution import resolve_escalation
from sop.state import SuspectedCause
from sop.supply_coordination_agent import notify_line_changed
from sop.validation_agent import ValidationRules, process_pending_validations
from supply_fixtures import order
from datetime import date

pytestmark = pytest.mark.anyio

INSIDE_FREEZE = date(2017, 8, 18)


def always_failing(store):
    """입력과 상관없이 불합격을 내는 검증 규칙(배분 로직 오류를 대신한다)."""
    rules = ValidationRules(
        instance_id=lambda record: record.line_id,
        check=lambda record: [SuspectedCause(type="allocation", issue="exceeds_supply")], instance_kind="line",
    )
    return process_pending_validations(store, VALIDATOR, {SUPPLY: rules})


async def coordinated_store(capacity=600.0):
    store, data = make_store(instances=(("A", "I1"),), capacity={"L1": capacity, "L2": capacity}), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    await pump(agent)
    return store, data, agent


async def test_a_passed_conclusion_is_complete_and_sends_no_signal():
    store, data, agent = await coordinated_store()

    (verdict,) = validate(store, data)

    assert verdict.status == "passed" and line_state(store, "L1") == "completed"
    assert store.queue(SUPPLY).empty()


async def test_a_failed_conclusion_with_changed_inputs_is_coordinated_again_right_away():
    store, data, agent = await coordinated_store()
    await adjust_capacity_pool(store, "production_plan", "L1", -300.0, "라인 고장")  # 결론을 쓴 뒤 capacity가 줄었다
    (verdict,) = validate(store, data)
    assert verdict.status == "failed" and line_state(store, "L1") == "judged"

    results = await pump(agent)

    assert [r.outcome for r in results] == ["coordinated"]
    assert sum(entries_by_ref(store, "L1").values()) <= 300  # 줄어든 공급 안으로 다시 나눴다
    assert [e.payload["kind"] for e in decided(store)] == ["regular", "continuing"]
    (verdict,) = validate(store, data)
    assert verdict.status == "passed" and not store.state.escalation_records


async def test_a_failed_conclusion_with_the_same_inputs_is_a_logic_error_for_the_system_operator():
    store, data, agent = await coordinated_store()
    (verdict,) = always_failing(store)
    assert verdict.status == "failed"

    results = await pump(agent)

    assert [r.outcome for r in results] == ["invalid"]
    (escalation,) = store.state.escalation_records
    assert (escalation.reason, escalation.line_id, escalation.target_role) == ("allocation_invalid", "L1", "system_operator")
    assert line_state(store, "L1") == "awaiting_system" and len(decided(store)) == 1  # 다시 조율하지 않는다


async def test_a_rerun_of_the_invalid_conclusion_coordinates_the_line_again_without_comparing_inputs():
    store, data, agent = await coordinated_store()
    always_failing(store)
    await pump(agent)

    resolve_escalation(store, SYSTEM, 0, kind="rerun")  # 배분 로직을 고치고 다시 하라고 처리한다
    results = await pump(agent)

    assert [r.outcome for r in results][0] == "coordinated"
    assert len(decided(store)) == 2 and decided(store)[1].payload["kind"] == "continuing"  # 상태 신호와 처리 신호가 모두 와도 한 번만 조율한다
    assert line_state(store, "L1") == "awaiting_validation"
    (verdict,) = validate(store, data)
    assert verdict.status == "passed" and line_state(store, "L1") == "completed"


async def test_a_dismissed_invalid_conclusion_takes_the_line_out_of_this_cycle():
    store, data, agent = await coordinated_store()
    always_failing(store)
    await pump(agent)

    resolve_escalation(store, SYSTEM, 0, kind="dismiss")
    results = await pump(agent)
    assert [r.outcome for r in results][0] == "dismissed"

    data.add("L1", "orders", order("O1", "B", "I2", 60, INSIDE_FREEZE))  # 이후 변경도 이 라인을 다시 조율하지 않는다
    notify_line_changed(store, "L1")
    results = await pump(agent)

    assert [r.outcome for r in results] == ["dismissed"] and len(decided(store)) == 1


async def test_a_second_failure_after_the_rerun_makes_a_new_invalid_record():
    store, data, agent = await coordinated_store()
    always_failing(store)
    await pump(agent)
    resolve_escalation(store, SYSTEM, 0, kind="rerun")
    await pump(agent)

    always_failing(store)  # 다시 조율한 결론도 불합격이다
    results = await pump(agent)

    assert [r.outcome for r in results] == ["invalid"]
    assert [r.status for r in store.state.escalation_records] == ["resolved", "open"]  # 이전 처리는 새 판정의 처리가 아니다
