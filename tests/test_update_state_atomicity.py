"""`update_state`: 여러 필드와 로그 항목을 한 번의 State 갱신으로 쓴다(전부 쓰거나 하나도 쓰지 않는다). 각 쓰기 경로에서 쓰는
도중 예외가 나면 State가 이전 그대로이고 신호도 가지 않으며, 그 예외를 삼키지 않고 올리는지 확인한다.

[테스트 전용 입력] 쓰는 도중의 예외는 `update_state`가 로그 항목을 만드는 단계(필드를 모두 계산한 뒤)를 일부러 실패시켜 만든다.
"""

import inspect

import pytest

from assumption_fixtures import fixed_assumptions
from sop import access, forecast_steps
from sop.access import FieldWrite, LogWrite, PermissionDenied, StateStore
from sop.forecast_steps import ForecastRerunHandling
from sop.forecast_supply_allocation import (
    check_forecast_verdict,
    forward_validated_forecast,
    process_pending_allocations,
    run_forecast_select_and_submit,
    run_forecast_steps_select_and_submit,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.state import ForecastRecord, RolePermission, State
from sop.validation_agent import ValidationRules, process_pending_validations
from test_forecast_rerun import AGENT_ID, DEFAULT_ID, first_run, make
from validation_fixtures import FORECAST, SUPPLY, forecast_rules, make_store, validate_and_allocate

pytestmark = pytest.mark.anyio

RAMEN = "COMPANY-A:RAMEN"


def ramen_store() -> StateStore:
    return make_store([ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN")])


def real_store() -> StateStore:
    return make_store([ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")])


def queue_sizes(store: StateStore) -> dict[str, int]:
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


async def assert_nothing_is_applied(store: StateStore, monkeypatch, action, inbox: tuple[str, ...] = ()) -> None:
    """로그 항목을 만드는 단계에서 예외가 나게 한 뒤 `action`을 실행하고, State와 보낸 신호가 그대로인지 확인한다.
    `inbox`는 `action`이 일감을 꺼내 읽는 채널이다(꺼낸 신호는 줄어들 수 있고, 다른 채널로는 아무것도 보내지 않는다)."""
    before, signals = store.state, queue_sizes(store)

    def broken(*args, **kwargs):
        raise RuntimeError("쓰는 도중 낸 오류")

    monkeypatch.setattr(access, "LogEntry", broken)
    with pytest.raises(RuntimeError, match="쓰는 도중"):
        outcome = action()
        if inspect.isawaitable(outcome):
            await outcome
    assert store.state is before  # 기록도 로그 항목도 escalation 기록도 반영되지 않았다
    after = queue_sizes(store)
    assert {k: v for k, v in after.items() if k not in inbox} == {k: v for k, v in signals.items() if k not in inbox}
    assert all(after.get(k, 0) <= signals.get(k, 0) for k in inbox)  # 신호를 보내지 않았다


def narrow_history_around_default():
    value = next(a.value for a in first_run().assumptions if a.assumption_id == DEFAULT_ID)
    assert value is not None
    return lambda company_id, item_id: [value - 5, value + 5]


# --- update_state 자체 ------------------------------------------------------------------------------------


def commit_store() -> StateStore:
    permissions = [
        RolePermission(role_tag="f", field_path="forecast_records", access="w"),
        RolePermission(role_tag="f", field_path="escalation_records", access="w"),
        RolePermission(role_tag="f", field_path="role_logs.f", access="w"),
    ]
    records = [ForecastRecord(agent_id="C:I", company_id="C", item_id="I")]
    return StateStore(State(forecast_records=records, role_permissions=permissions))


def test_update_state_applies_every_write_at_once():
    store = commit_store()
    record = store.state.forecast_records[0].model_copy(update={"selection_basis": "rule"})

    entries = store.update_state(
        "f",
        fields=[FieldWrite("forecast_records[0]", record)],
        logs=[LogWrite("scenario_decided", "C:I", payload={"a": 1}), LogWrite("forecast_run_skipped", "C:I")],
    )

    assert store.state.forecast_records[0].selection_basis == "rule"
    assert [e.seq for e in entries] == [1, 2] and store.state.role_logs["f"] == entries


@pytest.mark.parametrize("missing", ["forecast_records", "escalation_records", "role_logs.f"])
def test_a_missing_permission_for_any_part_applies_nothing(missing):
    store = commit_store()
    store._state = store.state.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"role_permissions": [p for p in store.state.role_permissions if p.field_path != missing]}
    )
    before = store.state

    with pytest.raises(PermissionDenied):
        store.update_state(
            "f",
            fields=[FieldWrite("forecast_records[0]", before.forecast_records[0]), FieldWrite("escalation_records", [])],
            logs=[LogWrite("scenario_decided")],
        )

    assert store.state is before and queue_sizes(store) == {}


def test_a_write_that_fails_part_way_applies_nothing_and_the_exception_is_not_swallowed():
    store = commit_store()
    before = store.state

    with pytest.raises(IndexError):  # 둘째 필드가 없는 인스턴스 인덱스에 쓴다
        store.update_state(
            "f",
            fields=[FieldWrite("escalation_records", []), FieldWrite("forecast_records[5]", before.forecast_records[0])],
            logs=[LogWrite("scenario_decided")],
        )

    assert store.state is before and queue_sizes(store) == {}


async def test_a_state_write_failure_in_a_forecast_run_stops_the_run_instead_of_becoming_a_run_error():
    store = ramen_store()
    store._state = store.state.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"role_permissions": [
            p for p in store.state.role_permissions if not (p.role_tag == FORECAST and p.field_path == "forecast_records" and p.access == "w")
        ]}
    )
    before = store.state

    with pytest.raises(PermissionDenied):
        await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    assert store.state is before and not store.state.escalation_records  # forecast_run_error로 바꾸지 않는다


async def test_a_state_write_failure_after_the_computation_is_not_turned_into_a_run_error():
    store = real_store()
    store._state = store.state.model_copy(  # pyright: ignore[reportPrivateUsage]
        update={"role_permissions": [
            p for p in store.state.role_permissions if not (p.role_tag == FORECAST and p.field_path == "forecast_records" and p.access == "w")
        ]}
    )
    before = store.state

    with pytest.raises(PermissionDenied):
        await run_forecast_steps_select_and_submit(store, FORECAST, make())

    assert store.state is before  # 계산은 끝났지만 쓰지 못한 실행은 escalation 기록도 남기지 않고 멈춘다


# --- 각 쓰기 경로 ---------------------------------------------------------------------------------------------


async def test_the_normal_forecast_write_is_atomic(monkeypatch):
    store = ramen_store()

    await assert_nothing_is_applied(
        store, monkeypatch,
        lambda: run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN")),
    )


async def test_entering_a_hold_is_atomic(monkeypatch):
    store = ramen_store()

    await assert_nothing_is_applied(
        store, monkeypatch, lambda: run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", [])
    )


async def test_forwarding_a_passed_record_is_atomic(monkeypatch):
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    await assert_nothing_is_applied(store, monkeypatch, lambda: forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN"))


async def test_creating_an_allocation_candidate_is_atomic(monkeypatch):
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN")

    await assert_nothing_is_applied(store, monkeypatch, lambda: process_pending_allocations(store, SUPPLY), inbox=(SUPPLY,))


async def test_a_passed_verdict_is_atomic(monkeypatch):
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    await assert_nothing_is_applied(store, monkeypatch, lambda: process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules()), inbox=(VALIDATOR_ROLE_TAG,))


async def test_a_failed_verdict_is_atomic(monkeypatch):
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    await assert_nothing_is_applied(
        store, monkeypatch,
        lambda: process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda company, item: [1.0, 2.0])),
        inbox=(VALIDATOR_ROLE_TAG,),
    )


async def test_an_error_verdict_with_its_log_entry_and_escalation_is_atomic(monkeypatch):
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    rules = {FORECAST: ValidationRules(lambda record: record.agent_id, broken)}

    await assert_nothing_is_applied(store, monkeypatch, lambda: process_pending_validations(store, VALIDATOR_ROLE_TAG, rules), inbox=(VALIDATOR_ROLE_TAG,))


async def test_an_error_verdict_applies_the_verdict_the_escalation_and_the_log_together_and_only_the_human_is_signalled():
    store = ramen_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    store.queue(VALIDATOR_ROLE_TAG).get_nowait()

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    store.update_state(FORECAST, logs=[])  # 상태를 바꾸지 않는 갱신은 신호가 없다
    store.queue(VALIDATOR_ROLE_TAG).put_nowait({"field_path": "forecast_records[0]", "written_by": FORECAST})
    process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, broken)})

    (escalation,) = store.get_field(FORECAST, "escalation_records")
    verdict = store.get_field(FORECAST, "forecast_records[0]").validation
    (entry,) = [e for e in store.negotiation_log(FORECAST) if e.event == "validation_error"]
    assert verdict is not None and verdict.status == "error" and escalation.log_seq == entry.seq
    assert queue_sizes(store) == {"human_manager": 1}  # 기록이 사람 대기가 되어 판정됨 신호는 가지 않는다


async def test_a_rerun_write_with_its_rerun_log_entry_is_atomic(monkeypatch):
    store = real_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(narrow_history_around_default()))
    handling = ForecastRerunHandling(inputs, result)

    await assert_nothing_is_applied(store, monkeypatch, lambda: check_forecast_verdict(store, FORECAST, handling))


async def test_a_run_error_hold_is_atomic(monkeypatch):
    store = real_store()

    def failing(*args, **kwargs):
        raise RuntimeError("강제로 낸 실행 오류")

    monkeypatch.setattr(forecast_steps, "check_drivers", failing)

    await assert_nothing_is_applied(store, monkeypatch, lambda: run_forecast_steps_select_and_submit(store, FORECAST, make()))


async def test_the_normal_path_writes_the_record_and_its_log_entry_before_the_job_signal():
    store = ramen_store()

    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    record = store.get_field(FORECAST, "forecast_records[0]")
    (entry,) = store.negotiation_log(FORECAST)
    assert record.scenario is not None and entry.event == "scenario_decided"
    assert store.queue(VALIDATOR_ROLE_TAG).qsize() == 1 and queue_sizes(store) == {VALIDATOR_ROLE_TAG: 1}
    assert len(validate_and_allocate(store)) == 1
