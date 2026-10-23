"""`allocations`의 기록 상태와 신호(GRAPH_FLOW.md "신호 규칙"): 인스턴스는 라인(`line_id`)이고, 작성agent는 supply_coordination,
검증agent는 `validation_settings`의 `allocation_validation`이다. 카드에 `produces`가 없어 `passed`면 "완료"이고 신호가 가지 않는다.

[테스트 전용 입력] 배분 항목의 수량과 수요 식별자는 상태 계산에만 쓰이는 고정 값이다.
"""

import pytest

from sop.access import FieldWrite, StateStore
from sop.agent_cards import default_agent_cards
from sop.capacity import initial_allocations
from sop.escalation_records import new_escalation
from sop.state import Allocation, AllocationEntry, CapacityPool, RolePermission, State, SuspectedCause, ValidationResult
from sop.validation_settings import (
    ALLOCATION_VALIDATOR_ROLE_TAG,
    SUPPLY_COORDINATION_ROLE_TAG,
    default_validation_settings,
)

SUPPLY = SUPPLY_COORDINATION_ROLE_TAG
VALIDATOR = ALLOCATION_VALIDATOR_ROLE_TAG
_PERMISSIONS = [
    (SUPPLY, "allocations", "r"), (SUPPLY, "allocations", "w"), (SUPPLY, "escalation_records", "r"),
    (SUPPLY, "escalation_records", "a"), (SUPPLY, "role_logs.supply_coordination", "a"),
    (VALIDATOR, "allocations", "r"), (VALIDATOR, "allocations[*].validation", "w"),
]


def allocation(line_id: str, **kwargs) -> Allocation:
    entry = AllocationEntry(demand_ref="C:I", demand_kind="forecast", company_id="C", item_id="I", quantity=10, peg="soft")
    return Allocation(line_id=line_id, cycle="2017-08", entries=[entry], **kwargs)


def make_store(produces: str | None = None) -> StateStore:
    cards = [c.model_copy(update={"produces": produces}) if c.role_tag == SUPPLY else c for c in default_agent_cards()]
    permissions = [RolePermission(role_tag=r, field_path=f, access=a) for r, f, a in _PERMISSIONS]  # pyright: ignore[reportArgumentType]
    return StateStore(State(agent_cards=cards, validation_settings=default_validation_settings(), role_permissions=permissions))


def conclude(store: StateStore, *lines: str) -> None:
    """supply_coordination이 라인마다 첫 결론을 쓴다(아직 검증받지 않은 기록이 생긴다)."""
    store.set_field(SUPPLY, "allocations", [allocation(line, log_seq=1) for line in lines])


def pending(store: StateStore) -> dict[str, int]:
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


def drain(store: StateStore) -> None:
    for queue in store._queues.values():  # pyright: ignore[reportPrivateUsage]
        while not queue.empty():
            queue.get_nowait()


def test_a_new_conclusion_awaits_validation_by_the_validator_in_the_settings():
    store = make_store()

    conclude(store, "LINE-1")

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"
    assert store.record_owner(SUPPLY, "allocations[0]") == VALIDATOR
    assert pending(store) == {VALIDATOR: 1}
    assert store.queue(VALIDATOR).get_nowait()["record_path"] == "allocations[0]"


def test_a_passed_conclusion_is_completed_and_signals_nobody():
    store = make_store()
    conclude(store, "LINE-1")
    drain(store)

    store.update_state(VALIDATOR, fields=[_verdict("allocations[0]", "passed")])

    assert store.record_state(SUPPLY, "allocations[0]") == "completed"
    assert store.record_owner(SUPPLY, "allocations[0]") is None
    assert pending(store) == {}  # sales_channel이 읽어 간다. 신호는 없다


@pytest.mark.parametrize("status", ["failed", "error"])
def test_a_failed_or_error_verdict_is_judged_and_signals_the_author(status):
    store = make_store()
    conclude(store, "LINE-1")
    drain(store)

    store.update_state(VALIDATOR, fields=[_verdict("allocations[0]", status)])

    assert store.record_state(SUPPLY, "allocations[0]") == "judged"
    assert pending(store) == {SUPPLY: 1}


def test_a_passed_conclusion_is_judged_not_completed_when_the_author_card_produces_something():
    store = make_store(produces="다음 agent가 받는 것")
    conclude(store, "LINE-1")
    drain(store)

    store.update_state(VALIDATOR, fields=[_verdict("allocations[0]", "passed")])

    assert store.record_state(SUPPLY, "allocations[0]") == "judged"  # 넘길 때의 `forward_to`는 M5에서 allocations에 더한다
    assert pending(store) == {SUPPLY: 1}


def test_an_open_human_escalation_on_the_line_makes_only_that_line_wait_for_the_business():
    store = make_store()
    conclude(store, "LINE-1", "LINE-2")
    drain(store)
    hold = new_escalation(SUPPLY, "commitments_exceed_capacity", "하한이 공급 가능량을 넘음", line_id="LINE-1")

    store.set_field(SUPPLY, "escalation_records", [hold])

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_business"
    assert store.record_state(SUPPLY, "allocations[1]") == "awaiting_validation"  # 다른 라인은 영향받지 않는다
    assert pending(store) == {"human_manager": 1}


def test_an_open_system_escalation_on_the_line_makes_the_conclusion_wait_for_the_system():
    store = make_store()
    conclude(store, "LINE-1")
    drain(store)
    hold = new_escalation(SUPPLY, "allocation_invalid", "배분 로직 오류", line_id="LINE-1")

    store.set_field(SUPPLY, "escalation_records", [hold])

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_system"
    assert pending(store) == {"system_operator": 1}


def test_an_escalation_for_a_forecast_instance_does_not_hold_a_line():
    store = make_store()
    conclude(store, "LINE-1")
    hold = new_escalation("forecast", "no_computable_assumption", "x", agent_id="LINE-1")  # 같은 문자열이어도 인스턴스 종류가 다르다

    store.set_field(SUPPLY, "escalation_records", [hold])

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"


def test_a_resolved_or_notice_escalation_does_not_hold_the_line():
    store = make_store()
    conclude(store, "LINE-1")
    resolved = new_escalation(SUPPLY, "commitments_exceed_capacity", "x", line_id="LINE-1").model_copy(update={"status": "resolved"})
    notice = new_escalation(SUPPLY, "commitment_gap", "x", line_id="LINE-1")

    store.set_field(SUPPLY, "escalation_records", [resolved, notice])

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_validation"


def _verdict(path: str, status: str):
    causes = [SuspectedCause(type="allocation", issue="exceeds_supply")] if status == "failed" else []
    return FieldWrite(f"{path}.validation", ValidationResult(status=status, suspected_causes=causes, ts="t"))  # pyright: ignore[reportArgumentType]


# --- 시스템을 처음 설정할 때 만드는 빈 기록: 첫 조율 전의 보류도 기록 상태로 신호가 간다 --------------------------------------


def setup_store(*lines: str) -> StateStore:
    store = make_store()
    pools = [CapacityPool(line_id=line, total_capacity=100, remaining_capacity=100) for line in lines]
    store.set_field(SUPPLY, "allocations", initial_allocations(pools))
    return store


def test_initial_allocations_make_one_empty_record_per_line_and_have_no_state():
    store = setup_store("LINE-1", "LINE-2")

    records = store.state.allocations
    assert [(r.line_id, r.entries, r.cycle, r.validation, r.role_tag) for r in records] == [
        ("LINE-1", [], None, None, SUPPLY), ("LINE-2", [], None, None, SUPPLY),
    ]
    assert store.record_state(SUPPLY, "allocations[0]") == "none" and pending(store) == {}  # 빈 기록은 검증 대기가 아니다


def test_a_hold_on_a_line_before_its_first_conclusion_signals_the_human():
    store = setup_store("LINE-1", "LINE-2")
    hold = new_escalation(SUPPLY, "commitments_exceed_capacity", "하한이 공급 가능량을 넘음", line_id="LINE-1")

    store.set_field(SUPPLY, "escalation_records", [hold])

    assert store.record_state(SUPPLY, "allocations[0]") == "awaiting_business"
    assert store.record_state(SUPPLY, "allocations[1]") == "none"
    assert pending(store) == {"human_manager": 1}
    assert store.queue("human_manager").get_nowait()["record_path"] == "allocations[0]"
