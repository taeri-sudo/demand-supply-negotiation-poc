"""스케줄러(GRAPH_FLOW.md "동시성 모델"): 정해진 시각의 조율과 escalation 시한을 담당 채널로 알리고, 받는 쪽의 신호 처리 루프는 처리를 끝낸 신호만 확인한다.

[테스트 전용 입력] 시각은 주입한 값이다(실제로 기다리지 않는다). 처리 루프의 재전달만 짧은 실제 시간(0.05초)을 쓴다.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from coordination_fixtures import FORECAST, SCHEDULER, SUPPLY, Data, coordinator, deliver, make_store, pump
from sop.scheduler import DAILY_COORDINATION as SIGNAL_DAILY_COORDINATION, DEADLINE as SIGNAL_ESCALATION_DEADLINE
from sop.escalation_records import new_escalation
from sop.scheduler import Scheduler
from validation_fixtures import open_hold

pytestmark = pytest.mark.anyio

START = datetime(2017, 8, 15, 8, 0, tzinfo=timezone.utc)  # 정해진 조율 시각(09:00) 한 시간 전


def drain_kinds(store, channel):
    """채널에 쌓인 신호를 비우고 종류 이름으로 돌려준다. 기록을 가리키지 않는 신호는 정해진 조율 시각 신호다."""
    queue, kinds = store.queue(channel), []
    while not queue.empty():
        kinds.append(SIGNAL_ESCALATION_DEADLINE if "record_path" in queue.get_nowait() else SIGNAL_DAILY_COORDINATION)
    return kinds


def test_the_daily_coordination_signal_goes_to_supply_coordination_once_per_day():
    store = make_store()
    scheduler = Scheduler(store, START)

    assert scheduler.tick(START + timedelta(minutes=59)) == []  # 정해진 시각 전
    assert scheduler.tick(START + timedelta(hours=1)) == [(SUPPLY, SIGNAL_DAILY_COORDINATION)]
    assert scheduler.tick(START + timedelta(hours=2)) == []  # 같은 날 두 번 보내지 않는다
    assert drain_kinds(store, SUPPLY) == [SIGNAL_DAILY_COORDINATION]

    assert scheduler.tick(START + timedelta(days=1, hours=1)) == [(SUPPLY, SIGNAL_DAILY_COORDINATION)]  # 다음 날


def test_skipping_several_days_sends_a_single_signal():
    store = make_store()
    scheduler = Scheduler(store, START)

    assert scheduler.tick(START + timedelta(days=3)) == [(SUPPLY, SIGNAL_DAILY_COORDINATION)]
    assert drain_kinds(store, SUPPLY) == [SIGNAL_DAILY_COORDINATION]


def test_a_deadline_signal_goes_to_the_agent_that_created_the_escalation_once_the_deadline_passes():
    store = make_store(instances=(("A", "I1"), ("B", "I1")))
    open_hold(store, "A:I1", "selection_unresolved")  # forecast가 만든 시한이 있는 기록
    record = store.state.escalation_records[0]
    assert record.deadline is not None
    scheduler = Scheduler(store, datetime.fromisoformat(record.deadline) - timedelta(days=2))
    drain_kinds(store, FORECAST)

    deadlines = lambda sent: [pair for pair in sent if pair[1] == SIGNAL_ESCALATION_DEADLINE]  # noqa: E731
    assert deadlines(scheduler.tick(datetime.fromisoformat(record.deadline) - timedelta(seconds=1))) == []
    sent = scheduler.tick(datetime.fromisoformat(record.deadline))

    assert deadlines(sent) == [(FORECAST, SIGNAL_ESCALATION_DEADLINE)]
    message = store.queue(FORECAST).get_nowait()
    assert (message["record_path"], message["written_by"]) == ("escalation_records[0]", SCHEDULER)
    assert deadlines(scheduler.tick(datetime.fromisoformat(record.deadline) + timedelta(minutes=5))) == []  # 같은 시한은 한 번만 보낸다


def test_records_without_a_deadline_or_already_closed_get_no_deadline_signal():
    store = make_store(instances=(("A", "I1"), ("B", "I1")))
    open_hold(store, "A:I1", "forecast_run_error")  # system_operator 담당은 시한이 없다
    closed = new_escalation("forecast", "selection_unresolved", "x", agent_id="B:I1", deadline="2017-08-15T09:00:00+00:00").model_copy(
        update={"status": "expired"}
    )
    from sop.access import FieldWrite

    store.update_state(FORECAST, fields=[FieldWrite("escalation_records", [*store.state.escalation_records, closed])])
    drain_kinds(store, FORECAST)

    sent = Scheduler(store, START).tick(START + timedelta(days=5))

    assert [pair for pair in sent if pair[1] == SIGNAL_ESCALATION_DEADLINE] == []


def test_the_scheduler_reads_only_and_cannot_write_the_state():
    from sop.access import FieldWrite, PermissionDenied

    store = make_store()
    with pytest.raises(PermissionDenied):
        store.update_state(SCHEDULER, fields=[FieldWrite("escalation_records", [])])


def test_the_wait_until_the_next_signal_is_bounded():
    store = make_store()
    scheduler = Scheduler(store, START)

    assert scheduler.seconds_until_next(START) == 60.0  # 다음 조율 시각이 한 시간 뒤여도 새 시한을 보려고 60초 안에 깨어난다
    assert scheduler.seconds_until_next(START + timedelta(minutes=59, seconds=40)) == pytest.approx(20.0)


async def test_the_scheduler_task_sends_signals_as_time_passes():
    store = make_store()
    scheduler = Scheduler(store, START)
    now = [START]
    sleeps = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += timedelta(hours=1)  # 한 번 잘 때마다 한 시간이 흐른다
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run(lambda: now[0], fake_sleep)

    assert drain_kinds(store, SUPPLY) == [SIGNAL_DAILY_COORDINATION]  # 두 번째 깨어났을 때(09:00) 보냈다


# --- 받는 쪽의 신호 처리 루프: 처리를 끝낸 신호만 확인한다 -----------------------------------------------------------------------


async def test_the_run_loop_acknowledges_only_processed_signals_so_a_failed_one_comes_back():
    store, data = make_store(instances=(("A", "I1"),), ack_timeout=0.05), Data()
    data.failures = 1  # 첫 처리 중 외부 데이터 읽기가 실패한다
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    task = asyncio.create_task(agent.run())

    for _ in range(100):
        if store.state.allocations[0].cycle is not None:
            break
        await asyncio.sleep(0.02)
    task.cancel()

    assert store.state.allocations[0].cycle == "2017-08"  # 확인하지 않은 신호가 다시 나타나 처리됐다
    assert data.failures == 0
    assert store.queue(SUPPLY).in_flight_count() == 0 and store.queue(SUPPLY).empty()


async def test_a_signal_handled_twice_gives_the_same_result():
    store, data = make_store(instances=(("A", "I1"),)), Data()
    agent = coordinator(store, data)
    deliver(store, "A", "I1", 600)
    message = store.queue(SUPPLY).get_nowait()

    await agent.handle_signal(message)
    after_first = store.state.allocations[0]
    await agent.handle_signal(message)  # 같은 신호가 두 번 온다

    assert store.state.allocations[0] == after_first and len(
        [e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_decided"]
    ) == 1
    assert (await pump(agent)) == []
