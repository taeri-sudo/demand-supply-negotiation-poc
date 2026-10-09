"""신호 규칙(GRAPH_FLOW.md "신호 규칙"): 기록의 상태가 바뀔 때만 그 상태의 담당에게 신호가 가고, 받는 쪽은 자기 상태일 때만 일한다.

[테스트 전용 입력] 가정 값은 `assumption_fixtures`의 고정 가정을 쓴다.
"""

import pytest

from assumption_fixtures import fixed_assumptions
from sop.access import FieldWrite, StateStore
from sop.forecast_steps import ForecastRerunHandling
from sop.forecast_supply_allocation import (
    allocate_validated_forecast,
    check_forecast_verdict,
    forward_validated_forecast,
    run_forecast_select_and_submit,
    run_forecast_steps_select_and_submit,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.record_state import record_state
from sop.state import ForecastRecord, Scenario, SendBack, SuspectedCause, ValidationResult
from sop.validation_agent import process_pending_validations, validate_job
from validation_fixtures import FORECAST, SUPPLY, forecast_rules, make_store, open_hold, write_verdict

pytestmark = pytest.mark.anyio

RAMEN = "COMPANY-A:RAMEN"
JUDGED = "forecast"
SENT_BACK = "forecast"  # 판정됨과 되돌려짐 모두 작성agent의 채널(role_tag)로 간다


def ramen_store() -> StateStore:
    return make_store([ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN")])


def write_send_back(store: StateStore, path: str = "forecast_records[0]") -> None:
    """기록을 쓴 agent에게 되돌리는 쪽(supply_coordination)이 `send_back`을 쓴다."""
    send_back = SendBack(
        from_role=SUPPLY, suspected_causes=[SuspectedCause(type="method_selection")], ts="2026-10-09T00:00:00+00:00"
    )
    store.update_state(SUPPLY, fields=[FieldWrite(f"{path}.send_back", send_back)])


def signals(store: StateStore) -> dict[str, int]:
    """신호가 쌓인 채널과 개수(비어 있는 채널은 뺀다)."""
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


def drain(store: StateStore) -> None:
    for queue in store._queues.values():  # pyright: ignore[reportPrivateUsage]
        while not queue.empty():
            queue.get_nowait()


def with_scenario(record: ForecastRecord) -> ForecastRecord:
    return record.model_copy(update={"scenario": Scenario(value=1.0, assumption_ids=["a"], derivation="chosen")})


def state_of(store: StateStore, index: int = 0) -> str:
    return record_state(store.state.forecast_records[index], store.state.escalation_records)


async def submitted_store() -> StateStore:
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    return store


# --- 상태 계산 ---------------------------------------------------------------------------------------------


def test_the_state_is_computed_from_existing_values_by_priority():
    store = ramen_store()
    assert state_of(store) == "none"

    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))
    assert state_of(store) == "awaiting_validation"

    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))
    assert state_of(store) == "judged"

    store.set_field(FORECAST, "forecast_records[0].forward_to", SUPPLY)
    assert state_of(store) == "forwarded"

    write_send_back(store)
    assert state_of(store) == "sent_back"  # 전달됨보다 되돌려짐이 우선한다

    open_hold(store, RAMEN)
    assert state_of(store) == "awaiting_human"  # 사람 대기가 가장 우선한다


def test_forward_to_does_not_make_a_record_forwarded_unless_the_verdict_is_passed():
    store = ramen_store()
    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))
    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="error"))

    store.set_field(FORECAST, "forecast_records[0].forward_to", SUPPLY)

    assert state_of(store) == "judged"


# --- 보내는 쪽: 상태가 바뀔 때만, 그 상태의 담당에게만 ------------------------------------------------------------


def test_a_value_that_makes_a_record_wait_for_validation_signals_only_the_validator():
    store = ramen_store()

    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))

    assert signals(store) == {VALIDATOR_ROLE_TAG: 1}
    message = store.queue(VALIDATOR_ROLE_TAG).get_nowait()
    assert message["field_path"] == "forecast_records[0]" and message["written_by"] == FORECAST


def test_a_write_that_leaves_the_state_unchanged_sends_nothing():
    store = ramen_store()
    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))
    drain(store)

    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]).model_copy(update={"selection_basis": "rule"}))
    store.set_field(FORECAST, "forecast_records[0].selection_basis", "human")

    assert signals(store) == {}  # 검증 대기에서 검증 대기


def test_a_write_to_a_record_without_a_value_sends_nothing():
    store = ramen_store()

    store.set_field(FORECAST, "forecast_records[0].selection_basis", "rule")

    assert state_of(store) == "none" and signals(store) == {}


def test_logs_alone_never_send_a_signal():
    store = ramen_store()

    store.append_log(FORECAST, "forecast_run_skipped", agent_id=RAMEN)
    store.update_state(FORECAST, logs=[])

    assert signals(store) == {}


async def test_a_verdict_signals_only_the_author_with_the_record_path():
    store = await submitted_store()
    drain(store)

    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))

    assert signals(store) == {JUDGED: 1}
    message = store.queue(JUDGED).get_nowait()
    assert message["record_path"] == "forecast_records[0]" and message["field_path"] == "forecast_records[0]"


async def test_the_normal_completion_judgment_and_rerun_each_signal_only_their_own_owner():
    store = await submitted_store()
    assert signals(store) == {VALIDATOR_ROLE_TAG: 1}  # 정상 완료: 검증agent

    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda company, item: [1.0, 2.0]))
    assert signals(store) == {JUDGED: 1}  # 판정(failed): 작성agent

    drain(store)
    store.set_field(FORECAST, "forecast_records[0]", store.state.forecast_records[0].model_copy(update={"validation": None}))
    assert signals(store) == {VALIDATOR_ROLE_TAG: 1}  # 재실행해 다시 쓴 기록: 다시 검증agent


async def test_entering_a_hold_signals_only_the_human_and_never_the_validator():
    store = ramen_store()

    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", [])  # 계산된 가정 없음 → 보류

    assert signals(store) == {"human_manager": 1}


async def test_a_selection_that_cannot_be_resolved_signals_only_the_human():
    from sop.state import Assumption
    from assumption_fixtures import distinct

    store = ramen_store()
    split = distinct([
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ])

    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", split)

    assert signals(store) == {"human_manager": 1}


async def test_a_run_error_hold_without_a_record_write_signals_only_the_human(monkeypatch):
    from sop import forecast_steps
    from sop.forecast_supply_allocation import run_forecast_steps_select_and_submit
    from test_forecast_rerun import make

    def failing(*args, **kwargs):
        raise RuntimeError("강제로 낸 실행 오류")

    monkeypatch.setattr(forecast_steps, "check_drivers", failing)
    store = make_store([ForecastRecord(agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1")])

    await run_forecast_steps_select_and_submit(store, FORECAST, make())

    assert signals(store) == {"human_manager": 1}


async def test_an_error_verdict_signals_only_the_human():
    from sop.validation_agent import ValidationRules

    store = await submitted_store()

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, broken)})

    assert signals(store) == {"human_manager": 1}  # 판정됨 신호는 가지 않는다(사람 대기가 우선)


async def test_forwarding_signals_only_the_agent_named_in_forward_to():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    drain(store)

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True

    assert state_of(store) == "forwarded" and signals(store) == {SUPPLY: 1}  # 작성agent와 검증agent에게는 가지 않는다
    message = store.queue(SUPPLY).get_nowait()
    assert message["field_path"] == "forecast_records[0]" and message["written_by"] == FORECAST


async def test_a_record_forwarded_to_another_agent_signals_only_that_agent():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    drain(store)
    store._state = store.state.model_copy(update={"agent_cards": [  # pyright: ignore[reportPrivateUsage]
        card.model_copy(update={"known_agents": [*card.known_agents, "other_agent"]}) if card.role_tag == FORECAST else card
        for card in store.state.agent_cards
    ]})

    store.set_field(FORECAST, "forecast_records[0].forward_to", "other_agent")

    assert signals(store) == {"other_agent": 1}  # 채널은 담당 agent의 role_tag다
    message = store.queue("other_agent").get_nowait()
    assert allocate_validated_forecast(store, SUPPLY, message) is None  # forward_to가 자기가 아니다
    assert store.get_field(SUPPLY, "allocation_candidates") == []


async def test_a_record_that_is_already_forwarded_is_not_forwarded_again():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    drain(store)
    forwarded = store.state

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False

    assert store.state is forwarded and signals(store) == {}


async def test_a_send_back_signals_only_the_author():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN")
    drain(store)

    write_send_back(store)

    assert state_of(store) == "sent_back" and signals(store) == {SENT_BACK: 1}
    message = store.queue(SENT_BACK).get_nowait()
    assert message["record_path"] == "forecast_records[0]" and message["written_by"] == SUPPLY


async def test_a_rerun_result_returns_a_sent_back_record_to_awaiting_validation():
    from test_forecast_rerun import make

    inputs = make()
    store = make_store([ForecastRecord(agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1")])
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda company, item: [0.0, 1e9]))
    assert forward_validated_forecast(store, FORECAST, "CUST-01", "ITEM-1") is True
    write_send_back(store)
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    record = store.state.forecast_records[0]
    assert (record.validation, record.forward_to, record.send_back) == (None, None, None)
    assert state_of(store) == "awaiting_validation" and signals(store) == {VALIDATOR_ROLE_TAG: 1}
    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]
    assert entry.payload["trigger"] == "send_back" and entry.payload["suspected_cause_reasons"] == ["method_selection"]


# --- 받는 쪽: 자기 상태일 때만 일한다 --------------------------------------------------------------------------


async def test_the_validator_ignores_a_signal_for_a_record_that_is_already_judged():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    judged = store.state
    store.queue(VALIDATOR_ROLE_TAG).put_nowait({"field_path": "forecast_records[0]", "written_by": FORECAST})  # 늦게 도착한 신호

    results = process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    assert results == [] and store.state is judged


async def test_the_validator_ignores_a_signal_for_a_record_waiting_for_a_human():
    store = await submitted_store()
    open_hold(store, RAMEN)
    held = store.state

    result = validate_job(
        store, VALIDATOR_ROLE_TAG, {"field_path": "forecast_records[0]", "written_by": FORECAST}, forecast_rules()
    )

    assert result is None and store.state is held


async def test_the_validator_ignores_a_signal_for_a_record_without_a_value():
    store = ramen_store()

    result = validate_job(
        store, VALIDATOR_ROLE_TAG, {"field_path": "forecast_records[0]", "written_by": FORECAST}, forecast_rules()
    )

    assert result is None


async def test_the_author_ignores_a_verdict_signal_unless_the_record_is_judged():
    from test_forecast_rerun import first_run, make

    store = make_store([ForecastRecord(agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1")])
    handling = ForecastRerunHandling(make(), first_run())

    assert await check_forecast_verdict(store, FORECAST, handling) == "waiting"  # 값이 없다
    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))
    assert await check_forecast_verdict(store, FORECAST, handling) == "waiting"  # 검증 대기
    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))
    open_hold(store, "CUST-01:ITEM-1")
    before = store.state
    assert await check_forecast_verdict(store, FORECAST, handling) == "waiting"  # 사람 대기: 판정이 있어도 일하지 않는다
    assert store.state is before and forward_validated_forecast(store, FORECAST, "CUST-01", "ITEM-1") is False


async def test_supply_coordination_ignores_a_record_that_is_not_judged_as_passed():
    store = await submitted_store()
    message = {"field_path": "forecast_records[0]", "written_by": FORECAST}

    assert allocate_validated_forecast(store, SUPPLY, message) is None  # 검증 대기
    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))
    open_hold(store, RAMEN)
    assert allocate_validated_forecast(store, SUPPLY, message) is None  # 사람 대기(늦게 도착한 신호)
    assert store.get_field(SUPPLY, "allocation_candidates") == []


async def test_the_author_ignores_a_signal_for_a_forwarded_record():
    from test_forecast_rerun import first_run, make

    store = make_store([ForecastRecord(agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1")])
    handling = ForecastRerunHandling(make(), first_run())
    store.set_field(FORECAST, "forecast_records[0]", with_scenario(store.state.forecast_records[0]))
    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="passed"))
    assert forward_validated_forecast(store, FORECAST, "CUST-01", "ITEM-1") is True
    forwarded = store.state

    assert await check_forecast_verdict(store, FORECAST, handling) == "waiting"  # 늦게 도착한 판정 신호

    assert store.state is forwarded


async def test_the_validator_ignores_a_signal_for_a_sent_back_record():
    store = await submitted_store()
    write_send_back(store)
    sent_back = store.state

    result = validate_job(
        store, VALIDATOR_ROLE_TAG, {"field_path": "forecast_records[0]", "written_by": FORECAST}, forecast_rules()
    )

    assert result is None and store.state is sent_back


async def test_supply_coordination_ignores_a_record_that_was_sent_back_after_forwarding():
    store = await submitted_store()
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN")
    write_send_back(store)

    message = {"field_path": "forecast_records[0]", "written_by": FORECAST}
    assert allocate_validated_forecast(store, SUPPLY, message) is None
    assert store.get_field(SUPPLY, "allocation_candidates") == []
