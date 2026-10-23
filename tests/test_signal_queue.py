"""확인 응답 방식의 신호 큐 (GRAPH_FLOW.md "동시성 모델" 신호 전달).

확인 시간이 지나는 상황은 테스트가 큐에 넘긴 시계의 시각을 앞으로 돌려서 만든다. 실제로 그 시간만큼 기다리지 않는다.
"""

import asyncio

import pytest

from sop.access import StateStore
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.signal_queue import SignalQueue
from sop.state import ForecastRecord, Scenario, State
from sop.validation_agent import ValidationRules, process_pending_validations
from validation_fixtures import FORECAST, forecast_rules, make_store as make_forecast_store

pytestmark = pytest.mark.anyio


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def make(timeout=30.0):
    clock = Clock()
    return SignalQueue(clock, timeout), clock


def test_a_taken_signal_stays_in_flight_and_is_not_ready_until_the_timeout():
    queue, clock = make()
    queue.put_nowait({"n": 1})

    signal = queue.get_nowait()

    assert signal == {"n": 1} and queue.qsize() == 0 and queue.empty() and queue.in_flight_count() == 1
    clock.now = 29.9
    assert queue.empty() and queue.in_flight_count() == 1


def test_an_unacknowledged_signal_reappears_after_the_timeout_and_only_that_one():
    queue, clock = make()
    queue.put_nowait({"n": 1})
    queue.put_nowait({"n": 2})
    first, second = queue.get_nowait(), queue.get_nowait()
    assert queue.ack(second) is True  # 두 번째만 처리를 끝냈다

    clock.now = 30.0

    assert queue.qsize() == 1 and queue.in_flight_count() == 0
    assert queue.get_nowait() is first  # 확인하지 않은 신호만 다시 나타난다


def test_an_acknowledged_signal_never_comes_back():
    queue, clock = make()
    queue.put_nowait({"n": 1})
    signal = queue.get_nowait()

    assert queue.ack(signal) is True
    clock.now = 1000.0

    assert queue.empty() and queue.in_flight_count() == 0
    assert queue.ack(signal) is False  # 두 번째 확인은 지울 것이 없다


def test_a_late_acknowledgement_removes_the_signal_that_already_reappeared():
    queue, clock = make()
    queue.put_nowait({"n": 1})
    signal = queue.get_nowait()
    clock.now = 31.0
    assert queue.qsize() == 1  # 확인 기한이 지나 다시 나타났다

    assert queue.ack(signal) is True  # 늦게 도착한 확인이 다시 나타난 신호를 지운다

    assert queue.empty()


def test_a_reappeared_signal_keeps_its_content_and_can_be_taken_again_with_a_new_deadline():
    queue, clock = make()
    queue.put_nowait({"n": 1})
    queue.get_nowait()
    clock.now = 30.0

    again = queue.get_nowait()
    clock.now = 59.0

    assert again == {"n": 1} and queue.in_flight_count() == 1  # 새 확인 기한은 다시 꺼낸 시각부터 30초다
    clock.now = 60.0
    assert queue.qsize() == 1


def test_get_nowait_on_an_empty_queue_raises():
    queue, _ = make()
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


async def test_an_awaiting_get_wakes_up_for_a_new_signal():
    queue, _ = make()
    waiter = asyncio.create_task(queue.get())
    await asyncio.sleep(0)

    queue.put_nowait({"n": 7})

    assert await asyncio.wait_for(waiter, 1.0) == {"n": 7}


async def test_an_awaiting_get_receives_an_unacknowledged_signal_again_after_the_timeout():
    queue = SignalQueue(ack_timeout=0.05)  # 실제 시계로 짧게 기다린다
    queue.put_nowait({"n": 1})
    first = await queue.get()

    again = await asyncio.wait_for(queue.get(), 1.0)  # 확인하지 않아 다시 나타난다

    assert again is first
    queue.ack(again)
    assert queue.in_flight_count() == 0


def test_every_channel_of_a_store_follows_the_same_rule_with_its_injected_clock():
    clock = Clock()
    store = StateStore(State(), clock=clock, ack_timeout=10.0)
    store.queue("a").put_nowait({"n": 1})
    store.queue("human_manager").put_nowait({"n": 2})
    store.queue("a").get_nowait()
    store.queue("human_manager").get_nowait()

    clock.now = 10.0

    assert store.queue("a").qsize() == 1 and store.queue("human_manager").qsize() == 1


def test_a_consumer_that_fails_does_not_acknowledge_so_the_signal_is_processed_again():
    clock = Clock()
    base = make_forecast_store([ForecastRecord(agent_id="C:I", company_id="C", item_id="I")])
    store = StateStore(base.state, clock=clock, ack_timeout=30.0)
    record = store.state.forecast_records[0].model_copy(
        update={"scenario": Scenario(value=1.0, assumption_ids=["a"], derivation="chosen"), "selection_basis": "rule"}
    )
    store.set_field(FORECAST, "forecast_records[0]", record)  # 검증 대기가 되어 검증agent에게 신호가 간다

    def interrupted(record):
        raise KeyboardInterrupt  # 규칙 안의 예외(error 판정)가 아니라 처리 자체가 중단되는 경우

    with pytest.raises(KeyboardInterrupt):
        process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, interrupted)})
    assert store.queue(VALIDATOR_ROLE_TAG).in_flight_count() == 1  # 꺼냈지만 확인하지 않았다

    clock.now = 31.0
    assert store.queue(VALIDATOR_ROLE_TAG).qsize() == 1  # 확인 시간 뒤 다시 나타난다
    (verdict,) = process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda c, i: [0.0, 200.0]))

    assert verdict.status == "passed"
    clock.now = 1000.0
    assert store.queue(VALIDATOR_ROLE_TAG).empty() and store.queue(VALIDATOR_ROLE_TAG).in_flight_count() == 0  # 처리를 확인해 다시 오지 않는다
