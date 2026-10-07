"""forecast의 사람 escalation(`forecast->human_manager`) State 반영 — `no_computable_assumption`, `selection_unresolved`.

`options_exhausted`는 escalation 기록을 만드는 함수가 reason으로 받을 수 있는지만 확인한다(소진 조건은 정해지지 않았다).
"""

from datetime import date

import pandas as pd
import pytest

from sop import data_source_judgment
from sop.access import StateStore
from assumption_fixtures import fixed_assumptions
from sop.data_source_judgment import collect_instance_data, last_complete_month
from sop.forecast_assumption_calc import select_methods_and_calculate_values
from sop.forecast_assumption_definition import DEFAULT_ID, define_assumptions
from sop.forecast_driver_check import check_drivers
from sop.forecast_human_manager import (
    ESCALATION_EDGE,
    find_protocol_entry,
    new_forecast_escalation,
    no_computable_rationale,
    open_forecast_escalation,
    selection_unresolved_protocol_entry,
)
from sop.forecast_supply_allocation import run_forecast_select_and_allocate
from sop.promotion_episodes import Episode
from sop.state import Assumption, ForecastRecord, InteractionProtocol, RolePermission, State
from test_data_source_judgment import empty_similar, make_inputs as make_plain_inputs, orders_from_pos, pos_frame
from test_forecast_assumption_steps import make_inputs, run_pipeline

pytestmark = pytest.mark.anyio

RAMEN = "COMPANY-A:RAMEN"
SNACK = "COMPANY-A:SNACK"


def make_store() -> StateStore:
    permissions = [
        RolePermission(role_tag=role, field_path=field, access=access)
        for role, fields in (
            ("forecast", ("forecast_records", "negotiation_log", "escalation_records")),
            ("supply_coordination", ("allocation_candidates", "negotiation_log")),
        )
        for field in fields
        for access in ("r", "w")
    ]
    return StateStore(
        State(
            forecast_records=[
                ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN"),
                ForecastRecord(agent_id=SNACK, company_id="COMPANY-A", item_id="SNACK"),
            ],
            role_permissions=permissions,  # pyright: ignore[reportArgumentType] -- 리스트 컴프리헨션의 access가 str로 추론됨
        )
    )


def run(store, item_id, assumptions, rationale=None):
    return run_forecast_select_and_allocate(
        store, "forecast", "supply_coordination", "COMPANY-A", item_id, assumptions, rationale
    )


def split_assumptions():
    """값이 많이 갈리고(상대 범위 25%) 과거 정확도로도 1등을 가릴 수 없는 가정들 — 가정 선택 ③."""
    return [
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ]


def record_of(store, index):
    return store.get_field("forecast", f"forecast_records[{index}]")


# --- 계산된 가정이 하나도 없음: no_computable_assumption ------------------------------------------


async def test_no_computed_assumption_leaves_scenario_null_and_opens_exactly_one_intervention_record():
    store = make_store()

    candidate = await run(store, "RAMEN", [], "기본 가정까지 계산되지 않음")

    assert candidate is None
    record = record_of(store, 0)
    assert record.scenario is None and record.selection_basis is None
    (escalation,) = store.get_field("forecast", "escalation_records")
    assert escalation.agent_id == RAMEN
    assert escalation.trigger_edge == ESCALATION_EDGE
    assert escalation.reason == "no_computable_assumption"
    assert escalation.rationale == "기본 가정까지 계산되지 않음"
    assert escalation.mode == "intervention" and escalation.status == "open"
    assert escalation.target_role == "human_manager" and escalation.resolution is None


async def test_no_computed_assumption_sends_nothing_to_supply_coordination_or_validation():
    store = make_store()

    await run(store, "RAMEN", [])

    assert store.get_field("supply_coordination", "allocation_candidates") == []
    assert store.queue("allocation_candidates").empty()
    assert store.queue("validation").empty()  # 검증agent 일감이 만들어지지 않는다
    assert store.queue("escalation_records").qsize() == 1  # human_manager가 반응할 신호만 간다


async def test_other_instances_proceed_while_one_instance_is_held():
    store = make_store()

    held = await run(store, "RAMEN", [])
    proceeding = await run(store, "SNACK", fixed_assumptions("SNACK"))

    assert held is None and proceeding is not None
    assert proceeding.allocation == {SNACK: 80.0}
    assert record_of(store, 1).scenario is not None
    assert [e.agent_id for e in store.get_field("forecast", "escalation_records")] == [RAMEN]


async def test_zero_request_quantity_is_a_computed_value_not_an_escalation():
    store = make_store()

    candidate = await run(store, "RAMEN", [Assumption(assumption_id="a", value=0.0, forecast_uncertainty=1.0)])

    assert candidate is not None and candidate.allocation == {RAMEN: 0.0}
    scenario = record_of(store, 0).scenario
    assert scenario is not None and scenario.value == 0.0  # scenario.value = 0 (정상 값)
    assert store.get_field("forecast", "escalation_records") == []

    await run(store, "SNACK", [])
    assert record_of(store, 1).scenario is None  # scenario = null (escalation 있음)
    assert len(store.get_field("forecast", "escalation_records")) == 1


async def test_rerunning_with_the_same_snapshot_does_not_duplicate_the_open_record():
    store = make_store()

    await run(store, "RAMEN", [])
    await run(store, "RAMEN", [])

    assert len(store.get_field("forecast", "escalation_records")) == 1
    assert store.queue("escalation_records").qsize() == 1


async def test_same_reason_for_a_different_instance_is_a_separate_record():
    store = make_store()

    await run(store, "RAMEN", [])
    await run(store, "SNACK", [])

    assert [e.agent_id for e in store.get_field("forecast", "escalation_records")] == [RAMEN, SNACK]


async def test_a_computed_request_is_not_sent_while_an_open_record_exists_for_the_instance():
    store = make_store()
    await run(store, "RAMEN", [])

    candidate = await run(store, "RAMEN", fixed_assumptions("RAMEN"))

    assert candidate is None
    assert store.get_field("supply_coordination", "allocation_candidates") == []


# --- 요청량을 만들 수 없는 실제 경로: 계산 불가가 오류가 아니라 State 반영으로 이어진다 ---------------


def test_instance_with_no_orders_returns_unusable_instead_of_raising_and_later_steps_do_not_calculate():
    inputs = make_plain_inputs(
        pd.DataFrame({"order_date": pd.to_datetime([]), "quantity": [], "is_first_order": [], "promotion": []}),
        pos_frame(start="2014-04-01"),
    )

    collection = collect_instance_data(inputs)
    definition = define_assumptions(inputs)
    planning = last_complete_month(inputs.data_end)
    check = check_drivers(definition.assumptions, definition.premises, inputs, collection, planning)
    calculated = select_methods_and_calculate_values(check.assumptions, check.premises, inputs, collection, planning)

    assert collection.unusable_reason and collection.observed_start is None and collection.n_observed_months == 0
    assert collection.training_series.empty
    assert [j.judgment["decision"] for j in collection.judgments][-1] == "no_usable_orders"
    assert calculated.assumptions == [] and calculated.excluded_assumptions == []
    assert [a.assumption_id for a in check.assumptions] == [d.assumption_id for d in definition.assumptions]


def test_orders_only_after_the_last_complete_month_are_unusable():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.0)
    orders["order_date"] = pd.Timestamp("2017-08-10")  # 마지막 완전한 달(2017-07) 뒤의 불완전한 달
    inputs = make_plain_inputs(orders, pos)

    assert collect_instance_data(inputs).unusable_reason


def test_no_orders_left_after_use_from_is_unusable_and_keeps_the_orders_source_record(monkeypatch):
    pos = pos_frame(start="2014-04-01")
    inputs = make_plain_inputs(orders_from_pos(pos, "2014-08-01", first_factor=1.0), pos)
    late = Episode(
        start=pd.Timestamp("2029-01-01"), end=pd.Timestamp("2029-03-01"), days=60, baseline=1.0, run_level=2.0,
        ended=True, post_level=1.0, kind="promotion", reason="테스트", use_from=date(2030, 1, 1),
    )
    monkeypatch.setattr(data_source_judgment, "find_episodes", lambda weekly: [late])

    collection = collect_instance_data(inputs)

    assert collection.unusable_reason and "use_from" in collection.unusable_reason
    (source,) = collection.data_sources
    assert (source.kind, source.item_scope, source.use_from) == ("orders", "same_item", date(2030, 1, 1))


async def test_instance_with_no_usable_orders_ends_in_a_no_computable_assumption_record():
    inputs = make_plain_inputs(
        pd.DataFrame({"order_date": pd.to_datetime([]), "quantity": [], "is_first_order": [], "promotion": []}),
        pos_frame(start="2014-04-01"),
    )
    collection = collect_instance_data(inputs)
    definition = define_assumptions(inputs)
    planning = last_complete_month(inputs.data_end)
    check = check_drivers(definition.assumptions, definition.premises, inputs, collection, planning)
    calculated = select_methods_and_calculate_values(check.assumptions, check.premises, inputs, collection, planning)
    store = make_store()

    candidate = await run(
        store, "RAMEN", calculated.assumptions, no_computable_rationale(collection, calculated.excluded_assumptions)
    )

    assert candidate is None and record_of(store, 0).scenario is None
    (escalation,) = store.get_field("forecast", "escalation_records")
    assert escalation.reason == "no_computable_assumption" and "사용할 주문이 없음" in escalation.rationale


async def test_pipeline_where_no_method_can_be_computed_ends_in_a_no_computable_assumption_record(monkeypatch):
    from sop import forecast_method_selection as fms

    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", lambda *a, **k: float("nan"))
    _, collection, _, calculated = run_pipeline(make_inputs(1.0, 3))
    store = make_store()

    candidate = await run(
        store, "RAMEN", calculated.assumptions, no_computable_rationale(collection, calculated.excluded_assumptions)
    )

    assert calculated.assumptions == [] and candidate is None
    (escalation,) = store.get_field("forecast", "escalation_records")
    assert escalation.reason == "no_computable_assumption"
    assert f"가정 '{DEFAULT_ID}' 제외" in escalation.rationale


def test_rationale_collects_the_reasons_and_has_a_default():
    assert no_computable_rationale() == "계산된 가정이 하나도 없어 요청량을 만들 수 없음"


# --- 가정 선택 ③: selection_unresolved ------------------------------------------------------------


async def test_unresolved_selection_opens_a_record_keeps_the_median_and_does_not_send():
    store = make_store()

    candidate = await run(store, "RAMEN", split_assumptions())

    assert candidate is None
    assert store.get_field("supply_coordination", "allocation_candidates") == []
    record = record_of(store, 0)
    assert record.scenario is not None and record.scenario.derivation == "median" and record.scenario.value == 110.0
    assert record.selection_basis == "rule"
    (escalation,) = store.get_field("forecast", "escalation_records")
    assert escalation.reason == "selection_unresolved" and escalation.agent_id == RAMEN
    assert escalation.mode == "intervention" and escalation.status == "open" and escalation.rationale
    events = [e.event for e in store.get_field("forecast", "negotiation_log")]
    assert events == ["scenario_decided_median"]


async def test_a_clear_leader_does_not_open_a_record_even_if_values_differ_a_lot():
    store = make_store()
    clear_leader = [
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=5.0),
        Assumption(assumption_id="b", value=150.0, forecast_uncertainty=30.0),
    ]

    candidate = await run(store, "RAMEN", clear_leader)

    assert candidate is not None and candidate.allocation == {RAMEN: 100.0}
    assert store.get_field("forecast", "escalation_records") == []


async def test_rerunning_an_unresolved_selection_does_not_duplicate_the_record():
    store = make_store()

    await run(store, "RAMEN", split_assumptions())
    await run(store, "RAMEN", split_assumptions())

    assert [e.reason for e in store.get_field("forecast", "escalation_records")] == ["selection_unresolved"]


async def test_different_reasons_for_the_same_instance_are_separate_records():
    store = make_store()

    await run(store, "RAMEN", split_assumptions())
    await run(store, "RAMEN", [])

    assert [e.reason for e in store.get_field("forecast", "escalation_records")] == [
        "selection_unresolved",
        "no_computable_assumption",
    ]


# --- options_exhausted: escalation 기록을 만드는 함수가 받을 수 있게만 한다 -----------------------


def test_the_record_factory_accepts_options_exhausted():
    record = new_forecast_escalation(RAMEN, "options_exhausted", "보강할 데이터가 더 없음")
    assert record.reason == "options_exhausted" and record.mode == "intervention" and record.status == "open"


async def test_open_forecast_escalation_accepts_options_exhausted():
    store = make_store()

    record = open_forecast_escalation(store, "forecast", RAMEN, "options_exhausted", "보강할 데이터가 더 없음")

    assert record is not None
    assert [r.reason for r in store.get_field("forecast", "escalation_records")] == ["options_exhausted"]


# --- interaction_protocol: selection_unresolved 항목만 있다 ---------------------------------------


def test_protocol_has_only_the_selection_unresolved_entry_and_is_looked_up_by_edge_and_trigger():
    entry = selection_unresolved_protocol_entry()
    other_trigger_same_edge = InteractionProtocol(
        edge=ESCALATION_EDGE, scope=["forecast", "human_manager"], escalation_trigger="other"
    )
    entries = [other_trigger_same_edge, entry]

    assert find_protocol_entry(entries, ESCALATION_EDGE, "selection_unresolved") is entry
    assert find_protocol_entry(entries, ESCALATION_EDGE, "other") is other_trigger_same_edge
    assert find_protocol_entry([entry], ESCALATION_EDGE, "no_computable_assumption") is None
    assert find_protocol_entry([entry], ESCALATION_EDGE, "options_exhausted") is None
    assert find_protocol_entry([entry], "supply_coordination->human_manager", "selection_unresolved") is None
    assert entry.escalation_mode == "intervention" and entry.escalation_target == "human_manager"
    assert entry.escalation_kind == "rule" and entry.max_rounds is None and entry.repeat_escalation_threshold is None
