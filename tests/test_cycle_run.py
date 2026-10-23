"""계획 주기 한 번을 태스크로 끝까지 실행: forecast 인스턴스 → 검증 → 전달 → 라인별 정기 조율 → allocations 완료 (GRAPH_FLOW.md "동시성 모델").

[테스트 전용 입력] 합성 인스턴스는 `test_forecast_rerun.make()`의 입력에 (고객사, item)만 바꾼 것이고 라인·capacity·계약 조건은 fixture 값이다.
실제 샘플은 `data/generated`의 인스턴스 전체를 샘플 제공자로 돌린다.
"""

import asyncio
import dataclasses
from datetime import date
from functools import cache
from pathlib import Path

import pytest

from coordination_fixtures import SETTINGS, SUPPLY, VALIDATOR, Data, line_of, make_store, pump
from sop.access import signal
from sop.allocation_validation import allocation_validation_rules, snapshot_stock_provider
from sop.cycle_runner import ForecastInstances, run_planning_cycle, sample_instance_runtimes
from sop.escalation_resolution import resolve_escalation
from sop.forecast_supply_allocation import forward_validated_forecast
from sop.forecast_signals import ForecastInstanceRuntime
from sop.forecast_validation import VALIDATOR_ROLE_TAG as FORECAST_VALIDATOR
from sop.forecast_validation import forecast_validation_rules, monthly_order_history, snapshot_history_provider
from sop.supply_coordination_agent import SupplyCoordinator
from supply_fixtures import order
from test_forecast_rerun import make
from validation_fixtures import open_hold

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"
PAIRS = (("CUST-A", "I1"), ("CUST-B", "I1"), ("CUST-A", "I2"), ("CUST-A", "I3"))  # L1에 세 인스턴스, L2에 한 인스턴스
CAPACITY = {"L1": 400.0, "L2": 100.0}


def synthetic_runtimes() -> dict:
    return {(c, i): ForecastInstanceRuntime(dataclasses.replace(make(), company_id=c, item_id=i)) for c, i in PAIRS}


def history_of(company: str, item: str) -> list[float]:
    inputs = make()
    return monthly_order_history(inputs.orders, inputs.data_end)


@cache
def synthetic_cycle(start_order: tuple) -> dict:
    """합성 인스턴스로 한 주기를 돌린 결과 요약. 같은 `start_order`는 한 번만 돌린다."""

    async def run() -> dict:
        store, data = make_store(instances=PAIRS, capacity=CAPACITY), Data()
        data.set("L1", orders=[order("O1", "CUST-B", "I1", 60, date(2017, 8, 5))])  # 미납분이 있는 라인에서 경합한다
        agent = SupplyCoordinator(store, SETTINGS, data, line_of)
        instances = ForecastInstances(store, synthetic_runtimes())
        validators = {
            FORECAST_VALIDATOR: {"forecast": forecast_validation_rules(history_of)},
            VALIDATOR: {SUPPLY: allocation_validation_rules(store, VALIDATOR, data.stock)},
        }
        await run_planning_cycle(store, agent, instances, validators, start_order=start_order)
        state = store.state
        decided = [e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_decided"]
        return {
            "forecasts": {
                r.agent_id: (r.scenario.value if r.scenario else None, store.record_state(SUPPLY, f"forecast_records[{i}]"))
                for i, r in enumerate(state.forecast_records)
            },
            "allocations": {
                a.line_id: (
                    a.cycle, sorted((e.demand_ref, e.peg, e.quantity) for e in a.entries), a.validation.status if a.validation else None,
                    store.record_state(SUPPLY, f"allocations[{i}]"),
                )
                for i, a in enumerate(state.allocations)
            },
            "kinds": sorted((e.payload["line_id"], e.payload["kind"]) for e in decided),  # 어느 라인이 먼저 끝나는지는 실행 순서에 따른다
            "capacity": [p.remaining_capacity for p in state.capacity_pools],
            "escalations": [r.reason for r in state.escalation_records],
            "forwarded": sum(1 for e in store.negotiation_log(SUPPLY) if e.event == "forwarded"),
        }

    return asyncio.run(run())


def test_instances_sharing_a_line_run_together_and_the_line_is_coordinated_once_after_all_of_them_arrive():
    result = synthetic_cycle(PAIRS)

    assert all(state == "forwarded" and value is not None for value, state in result["forecasts"].values())  # 실행 → 검증 → 전달
    assert result["forwarded"] == 4 and result["escalations"] == []
    assert result["kinds"] == [("L1", "regular"), ("L2", "regular")]  # 처리 중인 인스턴스를 기다렸다가 라인마다 한 번
    for line, (cycle, entries, status, state) in result["allocations"].items():
        assert (cycle, status, state) == ("2017-08", "passed", "completed")
        assert sum(quantity for _, _, quantity in entries) <= CAPACITY[line] + 1e-9  # 공급 가능량(재고 0)을 넘지 않는다
    l1_refs = {ref for ref, _, _ in result["allocations"]["L1"][1]}
    assert "O1" in l1_refs and l1_refs <= {"CUST-A:I1", "CUST-B:I1", "CUST-A:I2", "O1"}  # 미납분이 들어갔다
    assert result["capacity"] == [400.0, 100.0]  # 조율은 capacity를 바꾸지 않는다


def test_the_allocation_does_not_depend_on_the_order_the_instance_tasks_start():
    reference = synthetic_cycle(PAIRS)

    assert synthetic_cycle(tuple(reversed(PAIRS))) == reference
    assert synthetic_cycle((PAIRS[2], PAIRS[0], PAIRS[3], PAIRS[1])) == reference


def test_a_signal_for_an_unknown_instance_is_acknowledged_and_a_known_one_is_routed_to_its_inbox():
    store = make_store(instances=PAIRS, capacity=CAPACITY)
    instances = ForecastInstances(store, synthetic_runtimes())

    assert instances.key_of(signal("forecast_records[1]", "x")) == ("CUST-B", "I1")
    assert instances.key_of(signal("forecast_records[9]", "x")) is None and instances.key_of({"at": "t"}) is None
    assert set(instances.inboxes) == set(PAIRS)  # 라우터를 시작하기 전에 모든 인스턴스의 인박스가 있다

    async def route() -> None:
        queue = store.queue("forecast")
        queue.put_nowait(signal("forecast_records[9]", "x"))
        queue.put_nowait(signal("forecast_records[2]", "x"))
        task = asyncio.create_task(instances.run_router())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(route())

    assert instances.inboxes[("CUST-A", "I2")].qsize() == 1  # 아는 인스턴스의 신호는 인박스에 들어가고 인스턴스가 처리할 때까지 확인하지 않는다
    assert store.queue("forecast").in_flight_count() == 1 and store.queue("forecast").qsize() == 0  # 모르는 신호는 확인해 큐에서 빠졌다


def test_an_instance_with_an_open_hold_is_skipped_with_a_log_and_a_dismissed_one_is_not_run():
    async def run():
        store = make_store(instances=PAIRS[:2], capacity=CAPACITY)
        open_hold(store, "CUST-A:I1", "forecast_run_error")  # 보류 중
        open_hold(store, "CUST-B:I1", "forecast_run_error")
        resolve_escalation(store, "system_operator", 1, kind="dismiss")  # 이번 주기에서 뺀다
        runtimes = {key: runtime for key, runtime in synthetic_runtimes().items() if key in PAIRS[:2]}
        instances = ForecastInstances(store, runtimes)
        tasks = [asyncio.create_task(instances.run_instance(key)) for key in instances.inboxes]
        await asyncio.sleep(0.2)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return store, runtimes

    store, runtimes = asyncio.run(run())

    skipped = [e for e in store.negotiation_log("forecast") if e.event == "forecast_run_skipped"]
    assert [e.agent_id for e in skipped] == ["CUST-A:I1"]
    assert all(r.scenario is None for r in store.state.forecast_records) and all(rt.handling is None for rt in runtimes.values())  # 실행하지 않았다


@pytest.mark.skipif(not (SAMPLE_DIR / "sample_meta.json").exists(), reason="data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다")
def test_a_whole_cycle_over_the_generated_sample_ends_with_completed_allocations():
    import pandas as pd

    from sop.capacity import load_initial_capacity_pools
    from sop.coordination_inputs import CoordinationSettings
    from sop.external_data import load_data_end, load_instances
    from sop.line_data_provider import SampleLineDataProvider

    async def run():
        as_of = load_data_end().date()
        provider = SampleLineDataProvider(as_of)
        frame = load_instances()
        pairs = tuple((str(c), str(i)) for c, i in zip(frame["company_id"], frame["item_id"]))
        pools = load_initial_capacity_pools()
        store = make_store(instances=pairs, capacity={p.line_id: p.total_capacity for p in pools})
        settings = CoordinationSettings(cycle="2017-08", as_of=as_of, cycle_end=date(2017, 8, 31))
        agent = SupplyCoordinator(store, settings, provider, provider.line_of)
        instances = ForecastInstances(store, sample_instance_runtimes(pairs))
        stock_of = snapshot_stock_provider(None, pd.Timestamp(as_of))
        validators = {
            FORECAST_VALIDATOR: {"forecast": forecast_validation_rules(snapshot_history_provider())},
            VALIDATOR: {SUPPLY: allocation_validation_rules(store, VALIDATOR, stock_of)},
        }
        await run_planning_cycle(store, agent, instances, validators)
        return store, stock_of, agent, provider

    store, stock_of, agent, provider = asyncio.run(run())

    held = {r.agent_id for i, r in enumerate(store.state.forecast_records) if store.record_state(SUPPLY, f"forecast_records[{i}]") == "awaiting_business"}
    assert held == {"CUST-44:ITEM-1047679", "CUST-44:ITEM-1083196"}  # 시장 데이터가 있어 유의한 category_trend 가정을 가진 둘은 가정 선택 ③으로 보류된다
    assert sorted(r.reason for r in store.state.escalation_records if r.mode == "intervention") == ["selection_unresolved"] * 2
    assert all(store.record_state(SUPPLY, f"forecast_records[{i}]") == "forwarded" for i, r in enumerate(store.state.forecast_records) if r.agent_id not in held)
    for index, allocation in enumerate(store.state.allocations):
        capacity = next(p.remaining_capacity for p in store.state.capacity_pools if p.line_id == allocation.line_id)
        assert allocation.cycle == "2017-08" and store.record_state(SUPPLY, f"allocations[{index}]") == "completed", allocation.line_id
        assert sum(e.quantity for e in allocation.entries) <= stock_of(allocation.line_id) + capacity + 1e-9

    # 보류된 두 인스턴스를 사람이 처리하면 검증 면제 통과로 전달돼 이어지는 조율로 들어온다
    before = {a.line_id: {(e.demand_ref, e.peg): e.quantity for e in a.entries} for a in store.state.allocations}
    human_values = {"CUST-44:ITEM-1047679": 13236.0, "CUST-44:ITEM-1083196": 888.0}  # 보류됐던 규칙 값(13237, 889)에 가까운 값을 계약 단위(12)의 배수로 넣는다
    for index, escalation in enumerate(store.state.escalation_records):
        if escalation.reason == "selection_unresolved" and escalation.agent_id is not None:
            resolve_escalation(store, "human_manager", index, kind="value", value=human_values[escalation.agent_id], unit_of=provider.unit)
    for company, item in (("CUST-44", "ITEM-1047679"), ("CUST-44", "ITEM-1083196")):
        assert forward_validated_forecast(store, "forecast", company, item)
    assert store.queue(FORECAST_VALIDATOR).qsize() == 0  # 사람이 넣은 값은 검증agent에게 가지 않는다
    asyncio.run(pump(agent))

    after = {a.line_id: {(e.demand_ref, e.peg): e.quantity for e in a.entries} for a in store.state.allocations}
    assert after["LINE-BEV-1"][("CUST-44:ITEM-1047679", "soft")] > 0 and after["LINE-FOOD-1"][("CUST-44:ITEM-1083196", "soft")] > 0
    bev_moved = {ref for ref, peg in after["LINE-BEV-1"] if after["LINE-BEV-1"][ref, peg] < before["LINE-BEV-1"].get((ref, peg), 0.0)}
    assert bev_moved and {ref.split(":")[-1] for ref in bev_moved if ":" in ref} <= {"ITEM-1047679"}  # 줄어든 예측 수요는 같은 item을 쓰는 다른 고객사뿐이다
    for ref, peg in before["LINE-BEV-1"]:
        if ref.endswith("ITEM-269036") or "ITEM-269036" in ref:
            assert after["LINE-BEV-1"][ref, peg] == before["LINE-BEV-1"][ref, peg]  # 다른 item은 그대로다
    for line in ("LINE-FOOD-2", "LINE-FROZEN-1", "LINE-LIQ-1"):
        assert after[line] == before[line]  # 다른 라인은 건드리지 않는다
