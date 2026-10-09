"""역할별 로그(`role_logs`)와 합쳐 읽은 `negotiation_log`, forecast 실행 실패의 escalation.

[테스트 전용 입력] 실제 forecast 결과는 `test_forecast_rerun.py`의 합성 입력을 쓰고, 실패는 내부 단계 함수를 일부러
예외로 바꿔 만든다.
"""

import pytest
from pydantic import ValidationError

from assumption_fixtures import fixed_assumptions
from sop import forecast_steps
from sop.access import PermissionDenied, StateStore
from sop.escalation_records import urgency_of
from sop.forecast_steps import ForecastRerunHandling
from sop.forecast_supply_allocation import (
    check_forecast_verdict,
    forward_validated_forecast,
    run_forecast_select_and_submit,
    run_forecast_steps_select_and_submit,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.state import ForecastRecord, RolePermission, State
from sop.validation_agent import process_pending_validations
from test_forecast_rerun import AGENT_ID, DEFAULT_ID, TREND_ID, first_run, make
from validation_fixtures import FORECAST, SUPPLY, forecast_rules, make_store, take_verdict_signals, validate_and_allocate

pytestmark = pytest.mark.anyio


def one_instance_store() -> StateStore:
    return make_store([ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")])


def record_of(store: StateStore) -> ForecastRecord:
    return store.get_field(FORECAST, "forecast_records[0]")


def narrow_history_around_default():
    value = next(a.value for a in first_run().assumptions if a.assumption_id == DEFAULT_ID)
    assert value is not None
    return lambda company_id, item_id: [value - 5, value + 5]


# --- 로그 구조 ---------------------------------------------------------------------------------------------


async def test_the_values_before_a_rerun_overwrites_them_are_found_in_the_log_payload():
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(narrow_history_around_default()))
    before = record_of(store)
    assert before.validation is not None and before.validation.status == "failed" and before.scenario is not None
    take_verdict_signals(store)

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    after = record_of(store)
    assert after.assumptions != before.assumptions and after.validation is None  # 현재값은 덮어써졌다
    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]
    previous = entry.payload["previous"]
    assert entry.role_tag == FORECAST and entry.agent_id == AGENT_ID
    assert entry.payload["suspected_cause_reasons"] == ["assumption:value_out_of_range"]
    assert entry.payload["trigger"] == "failed_verdict"  # 불합격 판정이 계기
    assert [a["assumption_id"] for a in previous["assumptions"]] == [DEFAULT_ID, TREND_ID]
    assert previous["scenario"]["value"] == before.scenario.value
    assert previous["selection_basis"] == before.selection_basis
    assert previous["validation"]["status"] == "failed"
    assert previous["validation"]["suspected_causes"][0]["assumption_id"] == TREND_ID


async def test_the_validation_log_entry_carries_the_causes_and_the_rationale():
    store = one_instance_store()
    inputs = make()
    await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(narrow_history_around_default()))

    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.event == "validation_failed"]

    assert entry.role_tag == VALIDATOR_ROLE_TAG and entry.agent_id == AGENT_ID
    assert entry.payload["suspected_causes"][0]["issue"] == "value_out_of_range"
    validation = record_of(store).validation
    assert validation is not None
    assert entry.payload["rationale"] and entry.payload["validation_ts"] == validation.ts


async def test_the_merged_log_follows_seq_across_roles():
    store = make_store([ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN")])
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    validate_and_allocate(store)

    merged = store.negotiation_log(FORECAST)

    assert {e.role_tag for e in merged} == {FORECAST, VALIDATOR_ROLE_TAG, SUPPLY}
    assert [e.seq for e in merged] == list(range(1, len(merged) + 1))  # 역할을 가로질러 하나의 순번
    assert [e.event for e in merged] == [
        "scenario_decided", "validation_passed", "forwarded", "allocation_candidate_generated",
    ]
    assert "negotiation_log" not in State.model_fields  # 저장하지 않고 합쳐 읽는다
    assert set(store.state.role_logs) == {FORECAST, VALIDATOR_ROLE_TAG, SUPPLY}


def test_a_role_can_write_only_its_own_log():
    permissions = [
        RolePermission(role_tag=FORECAST, field_path=f"role_logs.{FORECAST}", access="w"),
        RolePermission(role_tag=SUPPLY, field_path=f"role_logs.{SUPPLY}", access="w"),
    ]
    store = StateStore(State(role_permissions=permissions))

    store.append_log(FORECAST, "scenario_decided", agent_id="A")
    store.append_log(SUPPLY, "allocation_candidate_generated")
    with pytest.raises(PermissionDenied):
        store.append_log("forecast_validation", "validation_passed")  # 자기 로그 쓰기 권한이 없는 역할
    with pytest.raises(PermissionDenied):
        store.set_field(FORECAST, f"role_logs.{SUPPLY}", [])  # 다른 역할의 로그
    with pytest.raises(PermissionDenied):
        store.role_log(FORECAST, SUPPLY)  # 읽기 권한도 따로 있어야 한다

    assert [e.role_tag for e in store.state.role_logs[FORECAST]] == [FORECAST]
    assert [e.role_tag for e in store.state.role_logs[SUPPLY]] == [SUPPLY]


def test_event_names_are_a_fixed_set_and_values_go_in_fields():
    store = StateStore(State(role_permissions=[RolePermission(role_tag=FORECAST, field_path="role_logs.forecast", access="w")]))

    with pytest.raises(ValidationError):
        store.append_log(FORECAST, "forwarded_to_supply_coordination_A_2026-01-01")  # pyright: ignore[reportArgumentType]


async def test_forwarding_is_decided_per_instance_by_the_record_state():
    records = [
        ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN"),
        ForecastRecord(agent_id="COMPANY-A:SNACK", company_id="COMPANY-A", item_id="SNACK"),
    ]
    store = make_store(records)
    for item in ("RAMEN", "SNACK"):
        await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", item, fixed_assumptions(item))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "SNACK") is True
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "SNACK") is False  # 이미 전달됨
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True  # 다른 인스턴스는 막지 않는다


# --- forecast 실행 실패 -----------------------------------------------------------------------------------


@pytest.fixture
def fail_in_check_drivers(monkeypatch):
    """`check_drivers`가 처음 `failures`번 예외를 낸다. 호출 횟수를 담은 dict를 반환한다."""

    def install(failures: int, module=forecast_steps, name="check_drivers"):
        state = {"calls": 0}
        original = getattr(module, name)

        def wrapper(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] <= failures:
                raise RuntimeError("강제로 낸 실행 오류")
            return original(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)
        return state

    return install


def failure_entries(store: StateStore):
    return [e for e in store.negotiation_log(FORECAST) if e.event == "forecast_run_error"]


async def test_a_first_run_that_fails_opens_an_escalation_at_once_and_leaves_the_record_as_it_was(fail_in_check_drivers):
    state = fail_in_check_drivers(failures=99)
    store = make_store([
        ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1"),
        ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN"),
    ])
    before = record_of(store)

    result, submitted = await run_forecast_steps_select_and_submit(store, FORECAST, make())

    assert state["calls"] == 1 and result is None and submitted is False  # 재시도하지 않는다
    assert record_of(store) == before  # 이전 값 그대로
    (entry,) = failure_entries(store)  # 실패한 단계와 예외 내용이 로그에 남는다
    assert entry.agent_id == AGENT_ID and entry.payload["step"] == "check_drivers" and entry.payload["run"] == "first_run"
    assert "RuntimeError" in entry.payload["exception"] and "attempt" not in entry.payload
    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert (escalation.reason, escalation.mode, escalation.status, escalation.agent_id, escalation.trigger_edge) == (
        "forecast_run_error", "intervention", "open", AGENT_ID, "forecast->human_manager",
    )
    assert "check_drivers" in escalation.rationale and "RuntimeError" in escalation.rationale
    assert urgency_of(escalation.reason) == "emergency"
    assert store.queue(VALIDATOR_ROLE_TAG).empty() and store.queue("human_manager").qsize() == 1
    # 그 인스턴스만 멈춘다: 새로 계산한 값도 보류되고 다른 인스턴스는 진행한다
    assert await run_forecast_select_and_submit(store, FORECAST, "CUST-01", "ITEM-1", fixed_assumptions("RAMEN")) is False
    assert await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN")) is True


async def test_a_rerun_that_fails_keeps_the_record_and_the_previous_result_and_is_not_retried(fail_in_check_drivers):
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(narrow_history_around_default()))
    handling = ForecastRerunHandling(inputs, result)
    before = record_of(store)
    state = fail_in_check_drivers(failures=99)

    assert await check_forecast_verdict(store, FORECAST, handling) == "run_error"

    assert state["calls"] == 1
    assert record_of(store) == before  # 판정도 이전 값 그대로(failed)
    assert [e.payload["run"] for e in failure_entries(store)] == ["rerun"]
    assert not [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]
    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert escalation.reason == "forecast_run_error" and escalation.agent_id == AGENT_ID
    # 멈춘 인스턴스는 판정이 다시 도착해도 재실행하지 않는다
    assert await check_forecast_verdict(store, FORECAST, handling) == "waiting"


# --- 보류 중에는 forecast 기록을 다시 쓰지 않는다 -----------------------------------------------------------------


def split_assumptions():
    from assumption_fixtures import distinct
    from sop.state import Assumption

    return distinct([
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ])  # 가정 선택 ③


def instance_store() -> StateStore:
    return make_store([ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN")])


async def run_ramen(store, assumptions):
    return await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", assumptions)


def entry_at(store: StateStore, seq: int):
    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.seq == seq]
    return entry


async def test_an_instance_with_an_open_intervention_is_not_run_and_only_the_skip_is_logged():
    store = instance_store()
    await run_ramen(store, [])  # no_computable_assumption: 보류에 들어간다
    held_record = store.get_field(FORECAST, "forecast_records[0]")
    escalations = store.get_field(FORECAST, "escalation_records")
    decided_before = [e for e in store.negotiation_log(FORECAST) if e.event == "scenario_decided"]

    assert await run_ramen(store, fixed_assumptions("RAMEN")) is False  # 계산하지 않는다
    assert await run_ramen(store, split_assumptions()) is False  # 다른 reason이 될 수 있는 값도 계산하지 않는다

    assert store.get_field(FORECAST, "forecast_records[0]") == held_record
    assert store.get_field(FORECAST, "escalation_records") == escalations  # 다른 reason의 escalation 기록을 더하지 않는다
    assert [e for e in store.negotiation_log(FORECAST) if e.event == "scenario_decided"] == decided_before
    skipped = [e for e in store.negotiation_log(FORECAST) if e.event == "forecast_run_skipped"]
    assert [e.payload["open_reasons"] for e in skipped] == [["no_computable_assumption"]] * 2
    assert store.queue(VALIDATOR_ROLE_TAG).empty() and store.queue("human_manager").qsize() == 1


async def test_a_first_run_for_a_held_instance_does_not_even_start(fail_in_check_drivers):
    state = fail_in_check_drivers(failures=0)  # 호출 횟수만 센다
    store = one_instance_store()
    await run_forecast_select_and_submit(store, FORECAST, "CUST-01", "ITEM-1", [])  # 보류에 들어간다
    held_record = record_of(store)

    result, submitted = await run_forecast_steps_select_and_submit(store, FORECAST, make())

    assert (result, submitted) == (None, False) and state["calls"] == 0  # 내부 단계를 하나도 돌리지 않았다
    assert record_of(store) == held_record and len(store.get_field(FORECAST, "escalation_records")) == 1
    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.event == "forecast_run_skipped"]
    assert entry.payload["run"] == "first_run" and entry.agent_id == AGENT_ID


async def test_a_hold_that_cannot_be_written_applies_none_of_the_log_the_escalation_and_the_record():
    from sop.access import PermissionDenied

    full = instance_store()
    without_log_permission = [
        p for p in full.state.role_permissions
        if not (p.role_tag == FORECAST and p.field_path == f"role_logs.{FORECAST}" and p.access == "w")
    ]
    store = StateStore(full.state.model_copy(update={"role_permissions": without_log_permission}))
    before = store.state

    with pytest.raises(PermissionDenied):
        await run_ramen(store, [])

    assert store.state is before  # 로그 항목도 escalation 기록도 forecast 기록도 반영되지 않았다


# --- escalation 기록의 log_seq ------------------------------------------------------------------------------


async def test_no_computable_assumption_points_at_the_log_entry_with_the_excluded_assumptions():
    store = instance_store()
    await run_ramen(store, [])

    (escalation,) = store.get_field(FORECAST, "escalation_records")

    assert escalation.log_seq is not None
    entry = entry_at(store, escalation.log_seq)
    assert entry.event == "scenario_not_computable" and entry.agent_id == "COMPANY-A:RAMEN"
    assert entry.payload["rationale"] == escalation.rationale and "excluded_assumptions" in entry.payload


async def test_selection_unresolved_points_at_the_log_entry_with_the_values_at_that_time():
    store = instance_store()
    await run_ramen(store, split_assumptions())

    (escalation,) = store.get_field(FORECAST, "escalation_records")

    assert escalation.reason == "selection_unresolved" and escalation.log_seq is not None
    entry = entry_at(store, escalation.log_seq)
    assert entry.event == "scenario_decided" and entry.payload["held"] is True
    assert entry.payload["value"] == 110.0 and entry.payload["assumption_values"] == {"a": 100.0, "b": 110.0, "c": 125.0}


async def test_options_exhausted_points_at_the_log_entry_with_the_kept_scenario_and_assumptions():
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda c, i: [1.0, 2.0]))  # 두 가정 모두 범위 밖
    kept = record_of(store)
    assert kept.scenario is not None

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert escalation.reason == "options_exhausted" and escalation.log_seq is not None
    entry = entry_at(store, escalation.log_seq)
    assert entry.event == "scenario_options_exhausted"
    assert entry.payload["scenario"]["value"] == kept.scenario.value
    assert [a["assumption_id"] for a in entry.payload["assumptions"]] == [DEFAULT_ID, TREND_ID]
    assert {e["assumption_id"] for e in entry.payload["excluded_assumptions"]} == {DEFAULT_ID, TREND_ID}


async def test_forecast_run_error_points_at_the_log_entry_with_the_exception(fail_in_check_drivers):
    fail_in_check_drivers(failures=99)
    store = one_instance_store()

    await run_forecast_steps_select_and_submit(store, FORECAST, make())

    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert escalation.reason == "forecast_run_error" and escalation.log_seq is not None
    entry = entry_at(store, escalation.log_seq)
    assert entry.event == "forecast_run_error" and "RuntimeError" in entry.payload["exception"]


async def test_validation_error_points_at_the_validator_log_entry():
    from sop.validation_agent import ValidationRules

    store = instance_store()
    await run_ramen(store, fixed_assumptions("RAMEN"))

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, broken)})

    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert escalation.reason == "validation_error" and escalation.log_seq is not None
    entry = entry_at(store, escalation.log_seq)
    assert entry.event == "validation_error" and entry.role_tag == VALIDATOR_ROLE_TAG
    assert "RuntimeError" in entry.payload["rationale"]
