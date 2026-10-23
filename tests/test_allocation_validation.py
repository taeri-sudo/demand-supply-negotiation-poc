"""`allocation_validation`: 배분 합계가 그 라인의 공급 가능량(재고 + 생산 중 물량 + 이번 주기 잔여 capacity)을 넘지 않는가.

[테스트 전용 입력] 라인의 결론은 직접 쓴 `Allocation`이고, 재고·생산 중 물량은 함수로 넘긴다(실제 제공 함수는 고정 샘플 테스트가 확인한다).
"""

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from sop.access import PermissionDenied
from sop.allocation_validation import allocation_validation_rules, snapshot_stock_provider
from sop.external_data import DEFAULT_DATA_DIR, load_inventory, load_line_assignments, load_work_in_progress, primary_line
from sop.state import Allocation, AllocationEntry, SourceRef, SuspectedCause, ValidationResult
from sop.validation_agent import ValidationRules, process_pending_validations
from supply_fixtures import SUPPLY, VALIDATOR, drain, make_store, pending

pytestmark = pytest.mark.anyio


def conclusion(line_id: str, *quantities: float) -> Allocation:
    entries = [
        AllocationEntry(demand_ref=f"D-{i}", demand_kind="forecast", company_id="A", item_id="I1", quantity=q, peg="soft")
        for i, q in enumerate(quantities)
    ]
    return Allocation(line_id=line_id, cycle="2017-08", entries=entries, log_seq=1)


def judged(store, stock: float, *quantities: float, line: str = "L1", index: int = 0) -> ValidationResult:
    store.set_field(SUPPLY, f"allocations[{index}]", conclusion(line, *quantities))
    rules = {SUPPLY: allocation_validation_rules(store, VALIDATOR, lambda line_id: stock)}
    (verdict,) = process_pending_validations(store, VALIDATOR, rules)
    return verdict


def test_a_total_within_stock_plus_remaining_capacity_passes_even_when_exactly_equal():
    store = make_store(capacity=400)

    assert judged(store, 200, 300, 300).status == "passed"  # 600 = 재고 200 + 잔여 capacity 400


def test_a_total_over_the_supply_fails_with_an_allocation_cause_and_signals_the_author():
    store = make_store(capacity=400)
    drain(store)
    store.set_field(SUPPLY, "allocations[0]", conclusion("L1", 300, 306))
    drain(store)
    rules = {SUPPLY: allocation_validation_rules(store, VALIDATOR, lambda line_id: 200)}

    store.queue(VALIDATOR).put_nowait({"field_path": "allocations[0]", "written_by": SUPPLY})
    (verdict,) = process_pending_validations(store, VALIDATOR, rules)

    assert verdict.status == "failed" and verdict.validator_role_tag == VALIDATOR
    assert [(c.type, c.issue, c.assumption_id, c.drivers) for c in verdict.suspected_causes] == [("allocation", "exceeds_supply", None, None)]
    assert "allocation:exceeds_supply" in (verdict.rationale or "")
    assert store.record_state(SUPPLY, "allocations[0]") == "judged" and pending(store) == {SUPPLY: 1}
    (entry,) = [e for e in store.negotiation_log(SUPPLY) if e.event == "validation_failed"]
    assert entry.agent_id is None and entry.payload["line_id"] == "L1"  # 라인 단위 기록은 agent_id 대신 payload의 line_id


def test_the_remaining_capacity_is_read_from_the_state_of_that_line():
    store = make_store(lines=("L1", "L2"), capacity=100)
    store._state = store.state.model_copy(update={  # pyright: ignore[reportPrivateUsage]
        "capacity_pools": [p.model_copy(update={"remaining_capacity": 1000}) if p.line_id == "L2" else p for p in store.state.capacity_pools]
    })

    assert judged(store, 0, 500, line="L1", index=0).status == "failed"  # L1은 잔여 100
    assert judged(store, 0, 500, line="L2", index=1).status == "passed"  # L2는 잔여 1000


def test_the_validation_does_not_reproduce_the_allocation_rules():
    """미납분·계약 하한·마진 순서를 지켰는지는 보지 않는다 — 합계만 본다. 규칙을 어긴 배분이어도 합계가 공급 안이면 통과다."""
    store = make_store(capacity=1000)

    verdict = judged(store, 0, 6, 600)  # 어떤 순서·비율이든 합계만 본다

    assert verdict.status == "passed"


def test_a_validation_that_raises_makes_a_line_based_system_escalation():
    store = make_store(capacity=1000)
    store.set_field(SUPPLY, "allocations[1]", conclusion("L2", 6))
    drain(store)

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    rules = {SUPPLY: ValidationRules(lambda r: r.line_id, broken, instance_kind="line")}
    store.queue(VALIDATOR).put_nowait({"field_path": "allocations[1]", "written_by": SUPPLY})
    (verdict,) = process_pending_validations(store, VALIDATOR, rules)

    assert verdict.status == "error"
    (escalation,) = store.state.escalation_records
    assert (escalation.reason, escalation.line_id, escalation.agent_id, escalation.target_role, escalation.trigger_edge) == (
        "validation_error", "L2", None, "system_operator", f"{VALIDATOR}->system_operator",
    )
    assert store.record_state(SUPPLY, "allocations[1]") == "awaiting_system" and pending(store) == {"system_operator": 1}
    assert store.record_state(SUPPLY, "allocations[0]") == "none"  # 다른 라인은 영향받지 않는다


def test_the_validator_writes_only_the_validation_field_of_allocations():
    store = make_store()
    store.set_field(SUPPLY, "allocations[0]", conclusion("L1", 6))

    store.set_field(VALIDATOR, "allocations[0].validation", ValidationResult(status="passed"))
    for forbidden in ("allocations[0]", "allocations[0].entries", "capacity_pools[0].remaining_capacity", "capacity_pools"):
        with pytest.raises(PermissionDenied):
            store.set_field(VALIDATOR, forbidden, None)


def test_the_allocation_cause_has_no_target_fields_and_only_the_exceeds_supply_issue():
    assert SuspectedCause(type="allocation", issue="exceeds_supply").assumption_id is None
    for bad in (
        dict(type="allocation"), dict(type="allocation", issue="no_evidence"), dict(type="allocation", issue="exceeds_supply", assumption_id="A"),
        dict(type="allocation", issue="exceeds_supply", drivers=["event"]), dict(type="data_source", issue="exceeds_supply"),
        dict(type="allocation", issue="exceeds_supply", source=SourceRef(kind="pos", item_scope="same_item")),
        dict(type="allocation", issue="exceeds_supply", use_from=date(2016, 1, 1)),
    ):
        with pytest.raises(ValidationError):
            SuspectedCause(**bad)  # pyright: ignore[reportArgumentType]


# --- 이번 주기 스냅샷의 재고·생산 중 물량 ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sample_stock():
    if not (DEFAULT_DATA_DIR / "line_assignments.csv").exists():
        pytest.skip("data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다")
    return snapshot_stock_provider()


def test_the_snapshot_stock_of_a_line_is_the_inventory_plus_work_in_progress_of_its_primary_items(sample_stock):
    assignments, end = load_line_assignments(), pd.Timestamp("2017-08-15")
    inventory, wip = load_inventory(), load_work_in_progress()
    for line in {primary_line(assignments, item, end) for item in set(assignments["item_id"])}:
        items = {i for i in set(assignments["item_id"]) if primary_line(assignments, i, end) == line}
        expected = inventory[inventory["item_id"].isin(items)]["quantity"].sum() + wip[wip["item_id"].isin(items)]["quantity"].sum()
        assert sample_stock(line) == pytest.approx(expected)


def test_a_line_unknown_to_the_snapshot_has_no_stock(sample_stock):
    assert sample_stock("LINE-UNKNOWN") == 0.0


# --- `allocation` 유형은 allocations의 판정에만 쓴다 ----------------------------------------------------------------------


def allocation_cause_result() -> ValidationResult:
    return ValidationResult(status="failed", suspected_causes=[SuspectedCause(type="allocation", issue="exceeds_supply")])


def test_an_allocation_cause_is_rejected_in_a_forecast_records_verdict_and_send_back():
    from sop.state import ForecastRecord, SendBack

    with pytest.raises(ValidationError):
        ForecastRecord(agent_id="A:I", company_id="A", item_id="I", validation=allocation_cause_result())
    with pytest.raises(ValidationError):
        SendBack(from_role="x", suspected_causes=[SuspectedCause(type="allocation", issue="exceeds_supply")], ts="t")


def test_the_wrapper_rejects_writing_an_allocation_cause_into_a_forecast_record_even_through_model_copy():
    from sop.access import CauseScopeRejected, FieldWrite
    from sop.state import ForecastRecord, RolePermission, SendBack, State
    from sop.access import StateStore

    permissions = [RolePermission(role_tag="w", field_path="forecast_records", access="w")]
    record = ForecastRecord(agent_id="A:I", company_id="A", item_id="I")
    store = StateStore(State(forecast_records=[record], role_permissions=permissions))
    before = store.state

    with pytest.raises(CauseScopeRejected):
        store.update_state("w", fields=[FieldWrite("forecast_records[0]", record.model_copy(update={"validation": allocation_cause_result()}))])
    send_back = SendBack.model_construct(from_role="x", suspected_causes=[SuspectedCause(type="allocation", issue="exceeds_supply")], ts="t")
    with pytest.raises(CauseScopeRejected):
        store.update_state("w", fields=[FieldWrite("forecast_records[0]", record.model_copy(update={"send_back": send_back}))])

    assert store.state is before  # 아무것도 쓰지 않았다


def test_an_allocations_verdict_carries_only_allocation_causes():
    with pytest.raises(ValidationError):
        Allocation(
            line_id="L1", cycle="2017-08",
            validation=ValidationResult(status="failed", suspected_causes=[SuspectedCause(type="method_selection")]),
        )
    store = make_store()
    store.set_field(SUPPLY, "allocations[0]", conclusion("L1", 6))
    from sop.access import CauseScopeRejected

    wrong = store.state.allocations[0].model_copy(
        update={"validation": ValidationResult.model_construct(status="failed", suspected_causes=[SuspectedCause(type="method_selection")])}
    )
    with pytest.raises(CauseScopeRejected):
        store.set_field(SUPPLY, "allocations[0]", wrong)
    store.set_field(VALIDATOR, "allocations[0].validation", allocation_cause_result())  # allocation 유형은 쓸 수 있다
