"""조율 흐름 (GRAPH_FLOW.md "조율 흐름"): 정기 조율을 시작하는 때, 정기 조율 전 변경, 입력 고정과 결론 직전의 확인, 이어지는 조율의 시점.

[테스트 전용 입력] 수요·capacity·계약 조건은 fixture 값이다(`coordination_fixtures.py`). 스냅샷 기준일은 2017-08-15, 변경 동결 구간은 2017-08-22까지다.
"""

from datetime import date

import pytest

from coordination_fixtures import (
    HUMAN,
    SUPPLY,
    SYSTEM,
    Data,
    coordinator,
    decided,
    deliver,
    entries_by_ref,
    forward_after_resolution,
    line_state,
    make_store,
    pump,
)
from sop.coordination_flow import InstanceStatus, NoProcessingInstances
from sop.escalation_resolution import resolve_escalation
from sop.supply_coordination_agent import notify_line_changed
from supply_fixtures import commitment, manual, order
from validation_fixtures import open_hold

pytestmark = pytest.mark.anyio

OVERDUE = date(2017, 8, 5)
INSIDE_FREEZE = date(2017, 8, 18)
OUTSIDE_FREEZE = date(2017, 8, 28)
DAILY = {"at": "2017-08-16T09:00:00+00:00"}  # 스케줄러의 정해진 조율 시각 신호(가리킬 기록이 없다)


def kinds(store, line_id="L1") -> list[str]:
    return [e.payload["kind"] for e in decided(store) if e.payload["line_id"] == line_id]


def send(agent, message) -> None:
    agent.store.queue(agent.role_tag).put_nowait(message)


def escalation_index(store, reason: str) -> int:
    return next(i for i, r in enumerate(store.state.escalation_records) if r.reason == reason)


# --- 정기 조율을 시작하는 때 ------------------------------------------------------------------------------------------------


async def test_the_regular_coordination_waits_for_the_instances_of_its_own_line_only():
    store, data = make_store(instances=(("A", "I1"), ("B", "I1"), ("A", "I3"))), Data()
    agent = coordinator(store, data)

    deliver(store, "A", "I1", 600)
    assert [r.outcome for r in await pump(agent)] == ["waiting"]  # B:I1이 아직 실행 전이라 처리 중이다
    assert store.state.allocations[0].cycle is None

    deliver(store, "A", "I3", 300)  # 다른 라인은 L1을 기다리지 않는다
    assert [r.outcome for r in await pump(agent)] == ["coordinated"]
    assert entries_by_ref(store, "L2") == {("A:I3", "soft"): 300} and store.state.allocations[0].cycle is None

    deliver(store, "B", "I1", 300)
    assert [r.outcome for r in await pump(agent)] == ["coordinated"]
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600, ("B:I1", "soft"): 300}
    assert kinds(store) == ["regular"] and kinds(store, "L2") == ["regular"]


@pytest.mark.parametrize("state", ["awaiting_validation", "judged", "sent_back"])
async def test_an_instance_waiting_for_validation_or_rerun_is_still_processing(state):
    from sop.state import ForecastRecord, Scenario, SendBack, SuspectedCause, ValidationResult
    from sop.access import FieldWrite

    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    base = ForecastRecord(
        agent_id="B:I1", company_id="B", item_id="I1", scenario=Scenario(value=300, assumption_ids=["a"], derivation="chosen"), selection_basis="rule",
    )
    update = {
        "awaiting_validation": {},
        "judged": {"validation": ValidationResult(status="failed", suspected_causes=[SuspectedCause(type="method_selection")], ts="2017-08-15T00:00:00+00:00")},
        "sent_back": {"send_back": SendBack(from_role=SUPPLY, suspected_causes=[SuspectedCause(type="method_selection")], ts="2017-08-15T00:00:00+00:00")},
    }[state]
    store.update_state("forecast", fields=[FieldWrite("forecast_records[1]", base.model_copy(update=update))])
    assert store.record_state(SUPPLY, "forecast_records[1]") == state

    assert [r.outcome for r in await pump(agent)] == ["waiting"]  # 재실행 중인 인스턴스의 결과가 같은 조율에 들어가도록 기다린다


async def test_an_instance_that_enters_a_hold_does_not_block_the_line_and_is_added_when_it_is_resolved():
    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    open_hold(store, "B:I1", "options_exhausted")  # 마지막 처리 중 인스턴스가 업무 대기로 빠졌다

    results = await pump(agent)

    assert [r.outcome for r in results] == ["coordinated", "idle"]  # 전달됨 신호로 시작하고, 보류 신호는 할 일이 없다
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600}  # 보류 중인 인스턴스의 몫은 비워 두지 않는다

    resolve_escalation(store, HUMAN, 0, kind="value", value=300.0, unit_of=lambda company, item: 6.0)
    forward_after_resolution(store, "B", "I1")
    results = await pump(agent)

    assert "coordinated" in [r.outcome for r in results]  # 사람의 처리 결과는 진행 중인 조율 직후에 들어온다
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600, ("B:I1", "soft"): 300}
    assert kinds(store) == ["regular", "continuing"]


async def test_the_last_processing_instance_leaving_by_hold_starts_the_regular_coordination_without_another_delivery():
    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    assert [r.outcome for r in await pump(agent)] == ["waiting"]  # B:I1이 아직 처리 중이다

    open_hold(store, "B:I1", "forecast_run_error")  # 마지막 처리 중 인스턴스가 시스템 대기로 빠진다 — 담당 채널은 system_operator다
    assert store.queue(SUPPLY).qsize() == 1  # 보류가 시작됐다는 신호가 supply_coordination에도 온다

    assert [r.outcome for r in await pump(agent)] == ["coordinated"]
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600}


async def test_a_dismissed_instance_is_not_waited_for_and_its_demand_is_left_out():
    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data)
    open_hold(store, "B:I1", "forecast_run_error")
    deliver(store, "A", "I1", 600)
    await pump(agent)
    assert store.state.allocations[0].cycle == "2017-08"

    resolve_escalation(store, SYSTEM, 0, kind="dismiss")  # 이번 주기에서 뺀다
    results = await pump(agent)

    assert [r.outcome for r in results] == ["idle"]  # 보류가 끝났다는 신호는 변경이 없어 조율하지 않는다
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600} and len(decided(store)) == 1


def test_the_start_rule_is_replaceable_with_the_same_input_and_output():
    class AlwaysReady:
        def ready(self, line_id, instances):
            return True

    statuses = [InstanceStatus("A:I1", "delivered", "forwarded"), InstanceStatus("B:I1", "processing", "none")]
    assert NoProcessingInstances().ready("L1", statuses) is False
    assert AlwaysReady().ready("L1", statuses) is True


async def test_a_replaced_start_rule_decides_when_the_regular_coordination_starts():
    class AlwaysReady:
        def ready(self, line_id, instances):
            return True

    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data, start_rule=AlwaysReady())
    deliver(store, "A", "I1", 600)

    assert [r.outcome for r in await pump(agent)] == ["coordinated"]  # B:I1이 처리 중이어도 규칙이 허락했다


# --- 정기 조율 전 변경 ----------------------------------------------------------------------------------------------------


async def test_a_change_signal_before_the_regular_coordination_does_nothing_and_the_change_goes_into_it():
    store, data = make_store(instances=(("A", "I1"), ("B", "I1"))), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    await pump(agent)
    data.add("L1", "orders", order("O1", "C", "I2", 60, INSIDE_FREEZE))  # 급한 변경이지만 정기 조율 전이다
    notify_line_changed(store, "L1")

    assert [r.outcome for r in await pump(agent)] == ["waiting"]
    assert len(decided(store)) == 0

    deliver(store, "B", "I1", 300)
    await pump(agent)

    assert kinds(store) == ["regular"]  # 변경은 정기 조율이 함께 넣는다
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600, ("B:I1", "soft"): 300, ("O1", "soft"): 60}


# --- 입력 고정과 결론 직전의 확인 ----------------------------------------------------------------------------------------------


async def test_a_value_arriving_during_the_computation_is_added_last_and_the_conclusion_is_written_once():
    store, data = make_store(instances=(("A", "I1"),)), Data()
    calls = []

    async def while_computing(line_id: str) -> None:
        if not calls:  # 계산하는 동안 사람이 정한 수량이 들어온다
            data.add("L1", "manual", manual("M1", None, "I2", 120))
            notify_line_changed(store, "L1")
        calls.append(line_id)

    agent = coordinator(store, data, compute_delay=while_computing)
    deliver(store, "A", "I1", 600)

    results = await pump(agent)

    assert [r.outcome for r in results] == ["coordinated", "idle"]  # 늦게 처리한 변경 신호는 새로 할 일이 없다
    assert len(decided(store)) == 1 and kinds(store) == ["regular"]  # 결론이 한 번만 써진다
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600, ("M1", "soft"): 120}
    (late,) = decided(store)[0].payload["late_changes"]
    assert (late["kind"], late["ref"], late["human"]) == ("manual", "M1", True)
    assert not [m for m in decided(store)[0].payload["inputs"]["forecasts"] if m["quantity"] == 120]  # 고정한 입력이 아니라 확인에서 읽은 입력을 남긴다
    assert [m["demand_id"] for m in decided(store)[0].payload["inputs"]["manual"]] == ["M1"]


async def test_a_value_arriving_after_the_final_check_goes_to_the_continuing_coordination_right_after():
    store, data = make_store(instances=(("A", "I1"),)), Data()
    agent = coordinator(store, data)
    # 읽기 순서: 라인 확인 1, 고정 2, 결론 직전의 확인 3, 결론을 쓴 뒤 라인 확인 4 — 4번째 읽기 직전에 값이 들어온다
    data.on_read[4] = lambda: data.add("L1", "manual", manual("M1", None, "I2", 120))
    deliver(store, "A", "I1", 600)

    await pump(agent)

    assert kinds(store) == ["regular", "continuing"]
    assert "M1" not in {e["demand_ref"] for e in decided(store)[0].payload["judgment"]["entries"]}
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600, ("M1", "soft"): 120}


async def test_a_line_check_that_reaches_the_round_limit_puts_its_own_signal_back_so_the_rest_is_not_lost(monkeypatch):
    from sop import supply_coordination_agent

    monkeypatch.setattr(supply_coordination_agent, "MAX_ROUNDS", 1)
    store, data = make_store(instances=(("A", "I1"),)), Data()
    agent = coordinator(store, data)
    data.on_read[4] = lambda: data.add("L1", "manual", manual("M1", None, "I2", 120))  # 첫 조율을 쓴 뒤 값이 들어온다
    deliver(store, "A", "I1", 600)
    message = store.queue(SUPPLY).get_nowait()

    await agent.handle_signal(message)
    store.queue(SUPPLY).ack(message)

    assert kinds(store) == ["regular"] and store.queue(SUPPLY).qsize() == 1  # 상한에 닿아 남은 변경을 못 봤지만 신호가 다시 들어 있다
    await pump(agent)
    assert kinds(store) == ["regular", "continuing"] and ("M1", "soft") in entries_by_ref(store, "L1")


# --- 이어지는 조율의 시점 ----------------------------------------------------------------------------------------------------


async def test_urgent_and_human_changes_are_coordinated_right_away_and_other_changes_wait_for_the_daily_time():
    store, data = make_store(instances=(("A", "I1"),)), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    await pump(agent)

    data.add("L1", "orders", order("O1", "B", "I2", 60, OUTSIDE_FREEZE))  # 일반 자동 변경
    notify_line_changed(store, "L1")
    assert [r.outcome for r in await pump(agent)] == ["deferred"] and kinds(store) == ["regular"]

    send(agent, DAILY)  # 정해진 시각
    assert [r.outcome for r in await pump(agent)][0] == "coordinated" and kinds(store) == ["regular", "continuing"]

    data.add("L1", "orders", order("O2", "B", "I2", 60, INSIDE_FREEZE))  # 변경 동결 구간 안 → 급한 변경
    notify_line_changed(store, "L1")
    await pump(agent)
    assert kinds(store)[-1] == "continuing" and len(kinds(store)) == 3

    data.add("L1", "manual", manual("M1", None, "I2", 12))  # 사람이 정한 수량
    notify_line_changed(store, "L1")
    await pump(agent)
    assert len(kinds(store)) == 4
    assert {ref for ref, _ in entries_by_ref(store, "L1")} == {"A:I1", "O1", "O2", "M1"}


async def test_a_coordination_gathers_every_pending_change_of_its_own_line_and_no_other_lines():
    store, data = make_store(instances=(("A", "I1"), ("A", "I3"))), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    deliver(store, "A", "I3", 300)
    await pump(agent)
    data.add("L1", "orders", order("O1", "B", "I2", 60, OUTSIDE_FREEZE), order("O2", "B", "I2", 60, OUTSIDE_FREEZE))
    data.add("L2", "orders", order("O3", "B", "I3", 60, OUTSIDE_FREEZE))

    notify_line_changed(store, "L1")  # L1의 변경 신호
    await pump(agent)
    send(agent, DAILY)
    await pump(agent)

    assert {ref for ref, _ in entries_by_ref(store, "L1")} == {"A:I1", "O1", "O2"}  # L1에 모인 변경이 모두 들어간다
    assert {ref for ref, _ in entries_by_ref(store, "L2")} == {"A:I3", "O3"}  # L2의 변경은 L2의 시점(하루 한 번)에 들어간다
    assert [e.payload["line_id"] for e in decided(store)[2:]] == ["L1", "L2"]


async def test_the_continuing_coordination_keeps_untouched_items_and_a_hard_peg_while_the_touched_item_gives_way():
    store, data = make_store(instances=(("A", "I1"), ("A", "I2")), capacity={"L1": 600.0, "L2": 600.0}), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 300)
    deliver(store, "A", "I2", 300)
    await pump(agent)
    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 300, ("A:I2", "soft"): 300}

    data.add("L1", "orders", order("O1", "B", "I1", 120, INSIDE_FREEZE))  # I1에만 닿는 급한 변경
    notify_line_changed(store, "L1")
    await pump(agent)

    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 210, ("A:I2", "soft"): 300, ("O1", "soft"): 84}  # I2는 그대로, I1끼리 비율로 나눈다

    data.set("L1", pegs={"A:I2": "hard"})  # 묶임이 바뀌는 변경은 영향 범위를 알 수 없어 약한 묶임 전체를 다시 나눈다
    send(agent, DAILY)
    await pump(agent)
    after = entries_by_ref(store, "L1")
    assert after[("A:I2", "hard")] == 300  # 강한 묶임은 옮겨지지 않는다
    assert sum(after.values()) <= 600


# --- 하한 초과·강한 묶임 초과와 사람의 처리 값 ---------------------------------------------------------------------------------


def exceeding_data(data):
    data.set("L1", orders=[order("ORD", "B", "I1", 600, OVERDUE)], commitments=[commitment("A", "I1", 3000)])


async def test_a_held_line_waits_for_the_human_and_the_resolution_values_stay_in_the_inputs_of_every_later_coordination():
    store, data = make_store(instances=(("A", "I1"),), capacity={"L1": 1000.0, "L2": 1000.0}), Data()
    exceeding_data(data)
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 1200)

    assert [r.outcome for r in await pump(agent)] == ["escalated"]
    assert line_state(store, "L1") == "awaiting_business" and store.state.allocations[0].cycle is None
    notify_line_changed(store, "L1")
    assert [r.outcome for r in await pump(agent)] == ["held"]  # 보류 중인 라인은 조율하지 않는다

    index = escalation_index(store, "commitments_exceed_capacity")
    resolve_escalation(store, HUMAN, index, kind="value", value={"A:I1": 300.0}, floor_unit_of=lambda ref: 6.0)  # 사람이 하한을 300으로 줄인다
    results = await pump(agent)

    assert "coordinated" in [r.outcome for r in results]  # 처리 결과는 처리 신호로 바로 반영된다
    assert store.state.allocations[0].cycle == "2017-08" and entries_by_ref(store, "L1")[("ORD", "soft")] == 600
    assert decided(store)[0].payload["inputs"]["floor_overrides"] == {"A:I1": 300.0}

    data.add("L1", "orders", order("O9", "C", "I2", 60, INSIDE_FREEZE))  # 이후 조율에도 사람이 정한 하한이 적용돼 같은 escalation이 다시 나지 않는다
    notify_line_changed(store, "L1")
    await pump(agent)
    assert kinds(store) == ["regular", "continuing"]
    assert [r.reason for r in store.state.escalation_records if r.reason == "commitments_exceed_capacity"] == ["commitments_exceed_capacity"]


async def test_a_human_value_for_a_demand_that_vanished_is_ignored_and_logged_at_coordination_time():
    from sop.access import FieldWrite
    from sop.escalation_records import new_escalation
    from sop.state import Resolution

    store, data = make_store(instances=(("A", "I1"),)), Data()
    agent = coordinator(store, data)
    gone = new_escalation(SUPPLY, "commitments_exceed_capacity", "x", line_id="L1").model_copy(
        update={"status": "resolved", "resolution": Resolution(resolved_by=HUMAN, ts="2017-08-15T00:00:00+00:00", kind="value", value={"GONE": 6.0})}
    )
    store.update_state(SUPPLY, fields=[FieldWrite("escalation_records", [gone])])  # 처리 뒤 그 수요가 사라졌다
    deliver(store, "A", "I1", 600)

    await pump(agent)

    assert entries_by_ref(store, "L1") == {("A:I1", "soft"): 600}  # 조율은 멈추지 않는다
    assert decided(store)[0].payload["ignored_resolution_refs"] == ["GONE"]


async def test_a_pegged_exceed_resolution_reduces_the_hard_quantity_in_the_next_coordination():
    store, data = make_store(instances=(("A", "I1"),), capacity={"L1": 400.0, "L2": 400.0}), Data()
    data.set("L1", pegs={"A:I1": "hard"})
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    assert [r.outcome for r in await pump(agent)] == ["escalated"]

    index = escalation_index(store, "pegged_exceed_capacity")
    resolve_escalation(store, HUMAN, index, kind="value", value={"A:I1": 240.0}, floor_unit_of=lambda ref: 6.0)
    await pump(agent)

    after = entries_by_ref(store, "L1")
    assert after[("A:I1", "hard")] == 240 and after.get(("A:I1", "soft"), 0) <= 160
