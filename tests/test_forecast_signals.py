"""forecast의 신호 처리: 담당 채널이 없는 상태로 돌아간 인스턴스를 escalation 처리 신호가 다시 일하게 한다 (GRAPH_FLOW.md "신호 규칙").

[테스트 전용 입력] 인스턴스 입력은 `test_forecast_rerun.make()`의 합성 입력이고, 가정 값은 `assumption_fixtures`의 고정 가정이다.
"""

from datetime import datetime, timedelta

import pytest

from assumption_fixtures import distinct
from sop import forecast_steps
from sop.access import FieldWrite
from sop.escalation_records import new_escalation
from sop.escalation_resolution import resolve_escalation
from sop.forecast_signals import ForecastInstanceRuntime, handle_forecast_signal
from sop.forecast_supply_allocation import run_forecast_select_and_submit, run_forecast_steps_select_and_submit
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.state import Assumption, ForecastRecord, SendBack, SuspectedCause
from sop.validation_agent import process_pending_validations
from test_forecast_rerun import make
from validation_fixtures import FORECAST, SYSTEM, forecast_rules, make_store

pytestmark = pytest.mark.anyio

AGENT_ID = "CUST-01:ITEM-1"
KEY = ("CUST-01", "ITEM-1")


def one_instance_store():
    return make_store([ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")])


def take(store, channel=FORECAST) -> list[dict]:
    queue, messages = store.queue(channel), []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


async def failed_first_run(store, monkeypatch):
    def failing(*args, **kwargs):
        raise RuntimeError("강제로 낸 실행 오류")

    original = forecast_steps.check_drivers
    monkeypatch.setattr(forecast_steps, "check_drivers", failing)
    await run_forecast_steps_select_and_submit(store, FORECAST, make())
    monkeypatch.setattr(forecast_steps, "check_drivers", original)  # 운영자가 원인을 고쳤다
    take(store)


async def test_a_first_run_that_failed_runs_again_when_the_system_operator_reruns_it(monkeypatch):
    store = one_instance_store()
    await failed_first_run(store, monkeypatch)
    assert store.record_state(FORECAST, "forecast_records[0]") == "awaiting_system"
    runtime = ForecastInstanceRuntime(make())

    resolve_escalation(store, SYSTEM, 0, kind="rerun")
    assert store.record_state(FORECAST, "forecast_records[0]") == "none"  # 처리하면 담당 채널이 없는 상태로 돌아간다
    messages = take(store)

    assert [m["record_path"] for m in messages] == ["forecast_records[0]"]  # 처리 신호가 그 인스턴스를 가리켜 forecast를 깨운다
    assert await handle_forecast_signal(store, messages[0], {KEY: runtime}) == "first_run"
    record = store.state.forecast_records[0]
    assert record.scenario is not None and store.record_state(FORECAST, "forecast_records[0]") == "awaiting_validation"
    assert runtime.handling is not None  # 재실행에 쓰는 직전 결과를 보관한다


async def test_a_dismissed_instance_is_not_run_again(monkeypatch):
    store = one_instance_store()
    await failed_first_run(store, monkeypatch)
    resolve_escalation(store, SYSTEM, 0, kind="dismiss")
    (message,) = take(store)
    runtime = ForecastInstanceRuntime(make())
    before = store.state

    assert await handle_forecast_signal(store, message, {KEY: runtime}) == "dismissed"

    assert store.state is before and runtime.handling is None  # 아무것도 쓰지 않고 실행하지 않는다


async def test_a_state_signal_for_a_dismissed_instance_does_not_rerun_it():
    store = one_instance_store()
    await run_forecast_steps_select_and_submit(store, FORECAST, make())  # 검증 대기까지 간 기록

    store.update_state("supply_coordination", fields=[FieldWrite("forecast_records[0].send_back", SendBack(
        from_role="supply_coordination", suspected_causes=[SuspectedCause(type="method_selection")], ts="2017-08-16T00:00:00+00:00"))])
    hold = new_escalation("forecast", "forecast_run_error", "재실행 실패", agent_id=AGENT_ID)
    store.update_state(FORECAST, fields=[FieldWrite("escalation_records", [hold])])  # 재실행이 실패해 시스템 대기
    resolve_escalation(store, SYSTEM, 0, kind="dismiss")  # 이번 주기에서 뺀다 → 기록은 다시 "되돌려짐"으로 계산된다
    assert store.record_state(FORECAST, "forecast_records[0]") == "sent_back"
    messages = take(store)

    results = [await handle_forecast_signal(store, m, {KEY: ForecastInstanceRuntime(make())}) for m in messages]

    assert set(results) == {"dismissed"}  # 상태 신호가 와도 다시 재실행하지 않는다
    assert not [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]


async def test_an_instance_that_expired_without_demand_is_not_run_again():
    store = one_instance_store()
    await run_forecast_select_and_submit(store, FORECAST, "CUST-01", "ITEM-1", [])  # 계산된 가정이 없어 보류
    deadline = datetime.fromisoformat(store.state.escalation_records[0].deadline)  # pyright: ignore[reportArgumentType]
    take(store)
    runtime = ForecastInstanceRuntime(make())

    expired = {"record_path": "escalation_records[0]", "at": (deadline + timedelta(hours=1)).isoformat()}
    assert await handle_forecast_signal(store, expired, {KEY: runtime}) == "expired"
    (closed,) = take(store)
    assert closed["record_path"] == "forecast_records[0]"

    assert await handle_forecast_signal(store, closed, {KEY: runtime}) == "no_demand"
    assert store.state.forecast_records[0].scenario is None and runtime.handling is None


async def test_the_deadline_signal_expires_only_a_record_whose_deadline_has_passed():
    store = one_instance_store()
    split = distinct([
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ])
    await run_forecast_select_and_submit(store, FORECAST, "CUST-01", "ITEM-1", split)  # 가정 선택 ③
    deadline = datetime.fromisoformat(store.state.escalation_records[0].deadline)  # pyright: ignore[reportArgumentType]
    message = {"record_path": "escalation_records[0]"}

    early = await handle_forecast_signal(store, {**message, "at": (deadline - timedelta(minutes=1)).isoformat()}, {})
    assert early == "skipped" and store.state.escalation_records[0].status == "open"

    late = await handle_forecast_signal(store, {**message, "at": deadline.isoformat()}, {})
    assert late == "expired" and store.state.escalation_records[0].status == "expired"
    assert store.state.forecast_records[0].selection_basis == "expiry"


async def test_a_judged_record_is_forwarded_by_the_state_signal():
    store = one_instance_store()
    await run_forecast_steps_select_and_submit(store, FORECAST, make())

    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda company, item: [0.0, 1e9]))  # 과거 범위 안이라 통과한다
    messages = take(store)

    assert [await handle_forecast_signal(store, m, {KEY: ForecastInstanceRuntime(make())}) for m in messages] == ["forwarded"]
    assert store.record_state(FORECAST, "forecast_records[0]") == "forwarded"


async def test_a_signal_for_an_instance_without_runtime_inputs_is_an_error():
    store = one_instance_store()

    with pytest.raises(ValueError):
        await handle_forecast_signal(store, {"record_path": "forecast_records[0]"}, {})


@pytest.mark.parametrize("keep_previous", [True, False])
async def test_a_rerun_without_the_previous_result_rebuilds_it_from_the_same_inputs(keep_previous):
    from sop.forecast_signals import run_first_run

    async def judged_and_rerun(keep: bool):
        store = one_instance_store()
        runtime = ForecastInstanceRuntime(make())
        await run_first_run(store, runtime)
        if not keep:
            runtime.handling = None  # 첫 실행 없이 재시작해 직전 결과가 메모리에 없다
        process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda company, item: [1.0, 2.0]))  # 두 가정 모두 범위 밖 → 불합격
        messages = take(store)
        outcomes = [await handle_forecast_signal(store, m, {KEY: runtime}) for m in messages]
        record = store.state.forecast_records[0]
        return outcomes, record.scenario, record.assumptions, [r.reason for r in store.state.escalation_records], runtime

    reference = await judged_and_rerun(True)
    outcome, scenario, assumptions, reasons, runtime = await judged_and_rerun(keep_previous)

    assert outcome == reference[0] and set(outcome) == {"rerun"}  # 직전 결과가 있든 없든 재실행이 일어난다
    assert (scenario, assumptions, reasons) == reference[1:4]  # 같은 입력이라 같은 결과다
    assert runtime.handling is not None  # 재실행에 쓴 직전 결과를 이 객체가 다시 보관한다
