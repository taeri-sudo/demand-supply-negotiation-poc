"""기록의 상태가 담당 채널이 없는 상태로 바뀌는 두 경우의 신호 (GRAPH_FLOW.md "신호 규칙"): 보류가 시작되거나 끝나면 다음 agent 후보에게,
escalation 기록이 처리되거나 시한 초과로 끝나면 그 기록을 만든 agent에게 간다. 신호는 모두 인스턴스의 기록(`forecast_records[i]`)을 가리킬 뿐
종류가 없다.
"""

import pytest

from sop.access import FieldWrite
from sop.escalation_resolution import resolve_escalation
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.state import ForecastRecord, Scenario, ValidationResult
from sop.validation_agent import ValidationRules, process_pending_validations
from validation_fixtures import FORECAST, HUMAN, SUPPLY, SYSTEM, make_store, open_hold, write_verdict

pytestmark = pytest.mark.anyio


def store_with_forwarded_record():
    store = make_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I")])
    record = store.state.forecast_records[0].model_copy(
        update={
            "scenario": Scenario(value=6.0, assumption_ids=["a"], derivation="chosen"), "selection_basis": "rule",
            "validation": ValidationResult(status="passed", ts="2017-08-15T00:00:00+00:00"), "forward_to": SUPPLY,
        }
    )
    store.update_state(FORECAST, fields=[FieldWrite("forecast_records[0]", record)])
    assert store.record_state(FORECAST, "forecast_records[0]") == "forwarded"
    return store


def take(store, channel) -> list[dict]:
    queue, messages = store.queue(channel), []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def test_a_hold_that_starts_tells_the_next_agent_candidates_of_the_author_card():
    store = store_with_forwarded_record()
    take(store, SUPPLY)

    open_hold(store, "C:I", "forecast_run_error")

    (message,) = take(store, SUPPLY)
    assert message["record_path"] == "forecast_records[0]"
    assert [m["record_path"] for m in take(store, SYSTEM)] == ["forecast_records[0]"]  # 담당(system_operator)도 같은 기록을 가리키는 신호를 받는다


def test_a_hold_that_ends_in_a_state_the_next_agent_owns_sends_it_one_signal_not_two():
    store = store_with_forwarded_record()
    open_hold(store, "C:I", "forecast_run_error")
    take(store, SUPPLY)

    resolve_escalation(store, SYSTEM, 0, kind="rerun")  # 보류가 끝나 기록이 다시 "전달됨"(담당: supply_coordination)이 된다

    assert store.record_state(FORECAST, "forecast_records[0]") == "forwarded"
    assert [m["record_path"] for m in take(store, SUPPLY)] == ["forecast_records[0]"]  # 상태 신호 하나만 간다
    assert [m["record_path"] for m in take(store, FORECAST)] == ["forecast_records[0]"]  # 만든 agent에게는 처리된 기록이 속한 인스턴스를 가리켜 알린다


def test_a_write_that_neither_starts_nor_ends_a_hold_sends_no_hold_signal():
    store = make_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I")])
    take(store, SUPPLY)

    store.set_field(FORECAST, "forecast_records[0].scenario", Scenario(value=6.0, assumption_ids=["a"], derivation="chosen"))
    store.set_field(FORECAST, "forecast_records[0].selection_basis", "rule")

    assert take(store, SUPPLY) == []


def test_a_closure_signal_goes_to_the_creator_with_a_card_and_not_to_a_validator():
    store = make_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I")])
    store.set_field(
        FORECAST, "forecast_records[0]",
        store.state.forecast_records[0].model_copy(
            update={"scenario": Scenario(value=6.0, assumption_ids=["a"], derivation="chosen"), "selection_basis": "rule"}
        ),
    )
    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, broken)})
    assert store.state.escalation_records[0].trigger_edge == f"{VALIDATOR_ROLE_TAG}->system_operator"
    take(store, VALIDATOR_ROLE_TAG)

    resolve_escalation(store, SYSTEM, 0, kind="rerun")

    assert len(take(store, VALIDATOR_ROLE_TAG)) == 1  # 검증 대기가 돼 상태 신호만 간다. 카드가 없어 처리 신호는 가지 않는다
    assert take(store, FORECAST) == []


def test_only_the_closed_record_sends_a_closure_signal():
    store = make_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I"), ForecastRecord(agent_id="D:I", company_id="D", item_id="I")])
    open_hold(store, "C:I", "options_exhausted")
    open_hold(store, "D:I", "options_exhausted")
    take(store, FORECAST)

    resolve_escalation(store, HUMAN, 0, kind="value", value=12.0, unit_of=lambda company, item: 6.0)

    assert {m["record_path"] for m in take(store, FORECAST)} == {"forecast_records[0]"}  # 처리한 기록의 인스턴스(C:I)만, D:I는 가리키지 않는다


def test_a_judgment_write_alone_never_sends_a_hold_or_closure_signal():
    store = make_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I")])
    store.set_field(
        FORECAST, "forecast_records[0]",
        store.state.forecast_records[0].model_copy(
            update={"scenario": Scenario(value=6.0, assumption_ids=["a"], derivation="chosen"), "selection_basis": "rule"}
        ),
    )
    take(store, SUPPLY)

    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))

    assert take(store, SUPPLY) == []
