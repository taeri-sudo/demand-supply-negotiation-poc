"""재실행: 의심되는 원인별 대상과 재개 지점, 수단 없음 → 가정 제외, 가정이 모두 제외됐을 때의 escalation 구분.

[테스트 전용 입력] 시장 지수와 POS 수준은 `test_forecast_assumption_steps.py`가 만든 합성 입력(연동 근거가 있는 경우)이고,
주문은 POS 월 합계에 비례하게 만들었다. 이력 길이만 `orders_start`로 바꾼다.
"""

from datetime import date
from functools import cache

import pandas as pd
import pytest
from pydantic import ValidationError

from sop import forecast_steps
from sop.access import StateStore
from sop.external_data import InstanceInputs
from sop.forecast_assumption_definition import (
    DEFAULT_ID,
    TREND_ID,
    AssumptionDefinition,
    redefine_assumptions,
    required_evidence_for,
)
from sop.forecast_supply_allocation import (
    rerun_forecast_steps_select_and_submit,
    run_forecast_steps_select_and_submit,
)
from sop.forecast_validation import monthly_order_history
from sop.forecast_steps import ForecastRerunHandling, ForecastStepsResult, run_forecast_steps
from sop.judgment import StructuredJudgment
from sop.state import (
    Assumption,
    Driver,
    Evidence,
    ForecastRecord,
    SourceRef,
    SuspectedCause,
)
from test_data_source_judgment import END, empty_similar, orders_from_pos
from test_forecast_assumption_steps import _market_and_pos
from validation_fixtures import forecast_rules, make_store as make_pipeline_store, validate_and_allocate

pytestmark = pytest.mark.anyio

AGENT_ID = "CUST-01:ITEM-1"
POS_SAME = SourceRef(kind="pos", item_scope="same_item")
MARKET = SourceRef(kind="market", item_scope="category")
ORDERS = SourceRef(kind="orders", item_scope="same_item")


def make(orders_start="2013-01-01", with_market=True) -> InstanceInputs:
    """주문 이력은 `orders_start`부터 2017-07까지다(2013-01은 55개월, 2015-01은 31개월, 2016-02는 18개월)."""
    market, pos = _market_and_pos(1.0, 3)
    return InstanceInputs(
        company_id="CUST-01",
        item_id="ITEM-1",
        family="DAIRY",
        industry="food_processing",
        perishable=False,
        data_end=END,
        orders=orders_from_pos(pos, orders_start, first_factor=1.0),
        pos_same=pos,
        pos_similar=empty_similar(),
        pos_category=pd.DataFrame({"month": [], "quantity": []}),
        market_index=market if with_market else None,
        market_group_index=market.to_frame("D152") if with_market else None,
    )


@cache
def first_run(orders_start="2013-01-01", with_market=True) -> ForecastStepsResult:
    return run_forecast_steps(make(orders_start, with_market))


def handling(orders_start="2013-01-01", with_market=True) -> ForecastRerunHandling:
    return ForecastRerunHandling(make(orders_start, with_market), first_run(orders_start, with_market))


def ids(result: ForecastStepsResult) -> list[str]:
    return [a.assumption_id for a in result.assumptions]


def methods(result: ForecastStepsResult, assumption_id: str) -> set[str]:
    return {m.method for a in result.assumptions if a.assumption_id == assumption_id for m in a.method_values}


def dropped(result: ForecastStepsResult) -> dict[str, list[str]]:
    return {e.assumption_id: e.reasons for e in result.dropped}


def cause(type_, issue=None, **kwargs) -> SuspectedCause:
    return SuspectedCause(type=type_, issue=issue, **kwargs)


# --- 같은 입력이면 같은 결과, 의심되는 원인이 주어지면 다른 결과 ----------------------------------------------


def test_same_snapshot_without_suspected_causes_gives_the_same_result():
    again = run_forecast_steps(make())
    before = first_run()

    assert ids(again) == ids(before) == [DEFAULT_ID, TREND_ID]
    assert [a.value for a in again.assumptions] == [a.value for a in before.assumptions]
    assert [m.method for a in again.assumptions for m in a.method_values] == [
        m.method for a in before.assumptions for m in a.method_values
    ]
    assert again.fingerprints == before.fingerprints


def test_same_snapshot_and_same_suspected_causes_give_the_same_result():
    suspected = cause("method_selection")
    one, two = handling().rerun(suspected), handling().rerun(suspected)

    assert methods(one, DEFAULT_ID) == methods(two, DEFAULT_ID)
    assert [a.value for a in one.assumptions] == [a.value for a in two.assumptions]


# --- 재개 지점: 의심되는 원인별로 어느 단계부터 다시 도는지(호출 횟수) ---------------------------------------


@pytest.fixture
def step_calls(monkeypatch):
    calls = {"redefine": 0, "collect": 0, "check": 0, "calculate": 0}

    def counted(name, function):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return function(*args, **kwargs)

        return wrapper

    for name, attribute in (
        ("redefine", "redefine_assumptions"),
        ("collect", "collect_instance_data"),
        ("check", "check_drivers"),
        ("calculate", "select_methods_and_calculate_values"),
    ):
        monkeypatch.setattr(forecast_steps, attribute, counted(name, getattr(forecast_steps, attribute)))
    return calls


@pytest.mark.parametrize(
    "causes, expected",
    [
        (cause("assumption", "value_out_of_range", assumption_id=TREND_ID),
         {"redefine": 1, "collect": 1, "check": 1, "calculate": 1}),  # 가정 정의부터
        (cause("data_source", "contaminated", source=POS_SAME),
         {"redefine": 0, "collect": 1, "check": 1, "calculate": 1}),  # 데이터 수집부터
        (cause("method_selection"),
         {"redefine": 0, "collect": 0, "check": 0, "calculate": 1}),  # 통계기법 선택부터, 수집 데이터 재사용
    ],
    ids=["assumption", "data_source", "method_selection"],
)
def test_the_suspected_cause_decides_the_resume_point(step_calls, causes, expected):
    handling().rerun(causes)

    assert step_calls == expected


# --- assumption: assumption_id의 가정이 대상, 원인을 모두 제외하고 원인이 없어진 가정은 수단 없음 ----------


def test_assumption_cause_excludes_all_drivers_of_the_target_and_the_driverless_assumption():
    result = handling().rerun(cause("assumption", "value_out_of_range", assumption_id=TREND_ID))

    assert ids(result) == [DEFAULT_ID] != ids(first_run())  # 직전과 다른 결과
    assert dropped(result) == {TREND_ID: ["assumption:value_out_of_range"]}
    assert result.dropped[0].rationale and result.defined.required_evidence == []  # 근거 목록도 새로 만든다
    (driver,) = result.dropped_drivers  # 원인 제외가 excluded_drivers에 의심되는 원인으로 남는다
    assert (driver.driver, driver.assumption_ids, driver.reasons) == (
        "category_trend", [TREND_ID], ["assumption:value_out_of_range"],
    )
    assert driver in result.excluded_drivers
    assert not any(d.driver == "category_trend" for a in result.defined.assumptions for d in a.drivers)


def test_assumption_cause_on_the_driverless_default_has_no_means_so_it_is_excluded():
    result = handling().rerun(cause("assumption", "not_distinct", assumption_id=DEFAULT_ID))

    assert ids(result) == [TREND_ID]  # 대상이 아닌 가정은 영향받지 않는다
    assert dropped(result) == {DEFAULT_ID: ["assumption:not_distinct"]}
    assert result.dropped_drivers == [] and "기본 가정" in result.dropped[0].rationale


def two_driver_definition() -> AssumptionDefinition:
    trend = Driver(driver="category_trend", evidence=Evidence(kind="market", item_scope="category", refs=["D152"]))
    event = Driver(driver="event", evidence=Evidence(kind="orders", item_scope="same_item", refs=["ITEM-1"]))
    two = Assumption(assumption_id="A-TWO", drivers=[trend, event])
    return AssumptionDefinition([two], [], required_evidence_for([two], []))


@pytest.mark.parametrize("issue", ["no_evidence", "double_counted"])
def test_a_cause_without_drivers_excludes_every_driver_of_the_assumption(issue):
    """`drivers`가 null이면 지목하지 못한 것이므로 그 가정의 모든 원인을 제외한다."""
    result = redefine_assumptions(two_driver_definition(), [cause("assumption", issue, assumption_id="A-TWO")])

    assert [(d.driver, d.reasons) for d in result.excluded_drivers] == [
        ("category_trend", [f"assumption:{issue}"]), ("event", [f"assumption:{issue}"]),
    ]
    assert [e.assumption_id for e in result.excluded_assumptions] == ["A-TWO"]
    assert result.definition.assumptions == [] and result.definition.required_evidence == []


@pytest.mark.parametrize("issue", ["no_evidence", "double_counted"])
def test_a_cause_with_drivers_excludes_only_those_drivers_and_keeps_the_assumption(issue):
    result = redefine_assumptions(
        two_driver_definition(), [cause("assumption", issue, assumption_id="A-TWO", drivers=["category_trend"])]
    )

    assert [(d.driver, d.reasons) for d in result.excluded_drivers] == [("category_trend", [f"assumption:{issue}"])]
    assert result.excluded_assumptions == []  # 원인이 남았으므로 수단이 있다
    (kept,) = result.definition.assumptions
    assert [d.driver for d in kept.drivers] == ["event"]
    assert result.definition.required_evidence == [("orders", "same_item")]


def test_excluding_every_named_driver_leaves_no_driver_so_the_assumption_has_no_means():
    result = redefine_assumptions(
        two_driver_definition(),
        [
            cause("assumption", "no_evidence", assumption_id="A-TWO", drivers=["category_trend"]),
            cause("assumption", "double_counted", assumption_id="A-TWO", drivers=["event"]),
        ],
    )

    assert [(d.driver, d.reasons) for d in result.excluded_drivers] == [
        ("category_trend", ["assumption:no_evidence"]), ("event", ["assumption:double_counted"]),
    ]
    assert [(e.assumption_id, e.reasons) for e in result.excluded_assumptions] == [
        ("A-TWO", ["assumption:no_evidence", "assumption:double_counted"])
    ]  # 가정을 제외하게 한 이유는 모두 남는다
    assert result.definition.assumptions == []


def test_a_driver_that_several_causes_cover_is_excluded_once_with_every_covering_cause_as_a_reason():
    result = redefine_assumptions(
        two_driver_definition(),
        [
            cause("assumption", "no_evidence", assumption_id="A-TWO", drivers=["event"]),
            cause("assumption", "value_out_of_range", assumption_id="A-TWO"),  # null = 모든 원인
        ],
    )

    assert [(d.driver, d.reasons) for d in result.excluded_drivers] == [
        ("category_trend", ["assumption:value_out_of_range"]),
        ("event", ["assumption:no_evidence", "assumption:value_out_of_range"]),
    ]


def test_assumption_cause_for_an_unknown_assumption_is_rejected():
    with pytest.raises(ValueError):
        handling().rerun(cause("assumption", "no_evidence", assumption_id="A-UNKNOWN"))


# --- data_source: 가정 ID가 없고 모든 가정이 각자 대응한다. 입력이 직전과 같으면 수단 없음 --------------


def test_insufficient_supplements_every_assumption_to_the_end_of_its_sources():
    before = first_run("2015-01-01")  # 31개월: 기준(24개월)을 넘어 보강하지 않았다
    assert len(before.collection.training_for(DEFAULT_ID)) == 31

    result = handling("2015-01-01").rerun(cause("data_source", "insufficient"))

    # 가정마다 값이 있는 달까지 끝까지 이어 붙인다: 같은 item의 POS(2013-01부터) 다음에 상위 단위인 시장 데이터(2012-01부터)
    assert len(result.collection.training_for(DEFAULT_ID)) == len(result.collection.training_for(TREND_ID)) == 67
    assert not result.collection.training_for(DEFAULT_ID).equals(result.collection.training_for(TREND_ID))  # 우선순위가 다르다
    assert result.dropped == [] and ids(result) == [DEFAULT_ID, TREND_ID]
    assert [a.value for a in result.assumptions] != [a.value for a in before.assumptions]  # 직전과 다른 결과


def test_insufficient_with_a_source_supplements_only_the_assumptions_that_use_that_source():
    before = first_run("2015-01-01")  # 31개월: 시장 소스를 쓰는 가정은 category_trend 가정뿐이다
    assert ("market", "category") not in before.collection.backcast_sources[DEFAULT_ID]

    result = handling("2015-01-01").rerun(cause("data_source", "insufficient", source=MARKET))

    assert len(result.collection.training_for(TREND_ID)) > 31  # 대상 가정은 끝까지 보강한다
    # 소스를 쓰지 않는 가정의 학습 시리즈와 입력은 바뀌지 않고 제외되지도 않는다
    assert result.collection.training_for(DEFAULT_ID).equals(before.collection.training_for(DEFAULT_ID))
    assert result.fingerprints[DEFAULT_ID] == before.fingerprints[DEFAULT_ID]
    assert ids(result) == [DEFAULT_ID, TREND_ID] and result.dropped == []


def test_insufficient_without_a_source_has_no_target_to_point_at_so_every_assumption_is_a_target():
    before = first_run("2015-01-01")

    result = handling("2015-01-01").rerun(cause("data_source", "insufficient"))

    for assumption_id in (DEFAULT_ID, TREND_ID):
        assert len(result.collection.training_for(assumption_id)) > len(before.collection.training_for(assumption_id))
        assert result.fingerprints[assumption_id] != before.fingerprints[assumption_id]


def test_insufficient_excludes_the_assumption_whose_input_does_not_change_whatever_the_history_length():
    # 시장 데이터가 없어 기본 가정 하나뿐이고, 주문(2013-01부터)보다 앞선 보강 데이터가 없다
    assert ids(first_run("2013-01-01", with_market=False)) == [DEFAULT_ID]

    result = handling("2013-01-01", with_market=False).rerun(cause("data_source", "insufficient"))

    assert result.assumptions == [] and dropped(result) == {DEFAULT_ID: ["data_source:insufficient"]}
    assert "입력이 직전 재실행과 같아" in result.dropped[0].rationale


def test_a_second_insufficient_cause_finds_nothing_more_to_add_and_excludes_the_assumptions():
    process = handling("2015-01-01")
    process.rerun(cause("data_source", "insufficient"))

    result = process.rerun(cause("data_source", "insufficient"))

    assert result.assumptions == []
    assert dropped(result) == {DEFAULT_ID: ["data_source:insufficient"], TREND_ID: ["data_source:insufficient"]}


def test_a_contaminated_source_is_excluded_and_only_assumptions_that_used_it_change():
    before = first_run("2016-02-01")  # 18개월: 보강이 필요하다
    assert ids(before) == [DEFAULT_ID, TREND_ID]

    result = handling("2016-02-01").rerun(cause("data_source", "contaminated", source=POS_SAME))

    assert [(e.kind, e.item_scope, e.reason) for e in result.collection.excluded_sources] == [
        ("pos", "same_item", "contaminated")
    ]
    assert ("pos", "same_item") not in {(s.kind, s.item_scope) for s in result.collection.data_sources}
    assert ids(result) == [DEFAULT_ID]  # 기본 가정은 다른 소스(시장)로 보강해 계속하고, 근거를 잃은 가정은 빠진다
    assert not result.collection.training_for(DEFAULT_ID).equals(before.collection.training_for(DEFAULT_ID))
    assert [(e.driver, e.reasons) for e in result.check.excluded_drivers] == [("category_trend", ["no_evidence"])]
    assert result.dropped == []


def test_contaminated_orders_cannot_be_excluded_so_the_input_stays_and_every_assumption_is_excluded():
    result = handling().rerun(cause("data_source", "contaminated", source=ORDERS))

    assert result.assumptions == [] and result.collection.excluded_sources == []
    assert dropped(result) == {DEFAULT_ID: ["data_source:contaminated"], TREND_ID: ["data_source:contaminated"]}
    assert ("orders", "same_item") in {(s.kind, s.item_scope) for s in result.collection.data_sources}  # 기본 데이터는 그대로


def test_outdated_orders_are_used_from_the_given_date():
    day = date(2014, 1, 1)
    before = first_run()

    result = handling().rerun(cause("data_source", "outdated", source=ORDERS, use_from=day))

    assert next(s for s in result.collection.data_sources if s.kind == "orders").use_from == day
    assert result.collection.observed_start == pd.Timestamp(day)
    assert result.collection.n_observed_months < before.collection.n_observed_months
    assert result.collection.excluded_sources == [] and ids(result) == [DEFAULT_ID, TREND_ID]


def test_outdated_market_changes_only_the_assumption_that_uses_the_market_source():
    result = handling().rerun(cause("data_source", "outdated", source=MARKET, use_from=date(2013, 6, 1)))

    market_source = next(s for s in result.collection.data_sources if s.kind == "market")
    assert market_source.use_from == date(2013, 6, 1)
    assert result.collection.evidence_series[("market", "category")].index.min() >= pd.Timestamp("2013-06-01")
    # 시장 소스를 쓰지 않는 기본 가정은 재실행의 대상이 아니라 입력이 같아도 제외하지 않는다
    assert DEFAULT_ID in ids(result) and DEFAULT_ID not in dropped(result)


def test_an_irrelevant_market_source_leaves_the_default_assumption_that_does_not_use_it():
    before = first_run()

    result = handling().rerun(cause("data_source", "irrelevant", source=MARKET))

    assert [(e.kind, e.item_scope, e.reason) for e in result.collection.excluded_sources] == [
        ("market", "category", "irrelevant")
    ]
    assert ("market", "category") not in {(s.kind, s.item_scope) for s in result.collection.data_sources}
    assert ids(result) == [DEFAULT_ID] and result.dropped == []  # 시장 소스를 쓰지 않는 기본 가정은 그대로 쓴다
    assert result.collection.training_for(DEFAULT_ID).equals(before.collection.training_for(DEFAULT_ID))
    assert [(e.driver, e.reasons) for e in result.check.excluded_drivers] == [("category_trend", ["no_evidence"])]


def test_a_source_the_assumption_does_not_use_is_not_a_target_even_when_it_is_contaminated():
    result = handling().rerun(cause("data_source", "contaminated", source=POS_SAME))  # 55개월: 기본 가정은 POS로 보강하지 않았다

    assert ids(result) == [DEFAULT_ID] and result.dropped == []
    assert [(e.kind, e.item_scope, e.reason) for e in result.collection.excluded_sources] == [
        ("pos", "same_item", "contaminated")
    ]


def test_an_assumption_whose_backcast_used_the_source_is_a_target():
    # 18개월: 기본 가정은 같은 item의 POS로 보강했으므로 그 소스를 쓰는 가정이다
    before = first_run("2016-02-01")
    assert ("pos", "same_item") in before.collection.backcast_sources[DEFAULT_ID]
    assert ("pos", "same_item") not in first_run().collection.backcast_sources[DEFAULT_ID]


def test_data_source_causes_that_the_schema_does_not_allow_are_rejected():
    with pytest.raises(ValidationError):  # orders는 항상 쓰므로 소스를 제외할 수 없다. orders는 outdated만 받는다
        cause("data_source", "irrelevant", source=ORDERS)
    with pytest.raises(ValidationError):  # outdated에는 use_from이 필수다
        cause("data_source", "outdated", source=ORDERS)
    with pytest.raises(ValidationError):  # irrelevant에는 use_from이 없다
        cause("data_source", "irrelevant", source=POS_SAME, use_from=date(2014, 1, 1))
    with pytest.raises(ValidationError):  # data_source에는 가정 ID가 없다
        cause("data_source", "contaminated", source=POS_SAME, assumption_id=DEFAULT_ID)
    with pytest.raises(ValidationError):
        cause("data_source", "contaminated")


# --- method_selection: 대상은 모든 가정, 한 번의 재실행 처리 동안 가정 안에서만 누적 ----------------


def test_method_selection_excludes_each_assumptions_previous_methods_and_selects_others():
    before = first_run()

    result = handling().rerun(cause("method_selection"))

    assert methods(before, DEFAULT_ID) and methods(result, DEFAULT_ID)
    assert methods(result, DEFAULT_ID).isdisjoint(methods(before, DEFAULT_ID))  # 직전과 다른 구성
    # 요인이 있는 가정은 회귀 계열만 쓸 수 있어 남은 구성이 없으면 제외된다
    assert TREND_ID in ids(result) and methods(result, TREND_ID).isdisjoint(methods(before, TREND_ID)) or TREND_ID in dropped(result)


def test_method_exclusions_accumulate_inside_one_handling_and_stay_inside_each_assumption():
    process = handling()
    first = process.result
    seen = {DEFAULT_ID: set(methods(first, DEFAULT_ID)), TREND_ID: set(methods(first, TREND_ID))}

    for _ in range(8):  # 가정이 모두 제외될 때까지(후보가 유한해 곧 끝난다)
        result = process.rerun(cause("method_selection"))
        for assumption_id, used in seen.items():
            current = methods(result, assumption_id)
            assert current.isdisjoint(used)  # 직전만이 아니라 지금까지 제외한 모든 기법을 다시 쓰지 않는다
            used |= current
        if not result.assumptions:
            break
    else:
        pytest.fail("기법 제외가 8번 안에 끝나지 않음")

    assert dropped(result) == {DEFAULT_ID: ["method_selection"], TREND_ID: ["method_selection"]}  # 남은 구성이 없으면 제외
    assert seen[DEFAULT_ID] & seen[TREND_ID]  # 다른 가정이 같은 기법을 쓰는 것은 문제없다
    assert "method_exclusions" not in ForecastRecord.model_fields  # State 필드가 아니다


def test_a_new_handling_starts_without_the_previous_exclusions():
    first = handling().rerun(cause("method_selection"))
    fresh = handling().rerun(cause("method_selection"))

    assert methods(first, DEFAULT_ID) == methods(fresh, DEFAULT_ID)


def test_method_selection_cause_has_no_target_field_because_every_assumption_is_the_target():
    with pytest.raises(ValidationError):
        cause("method_selection", assumption_id=DEFAULT_ID)


# --- 이유가 여러 개일 때: 가장 앞 재개 지점, 단계 순서대로 적용, 수단 없음 비교는 한 번 -----------------------------


def test_an_empty_cause_list_is_rejected():
    with pytest.raises(ValueError):
        handling().rerun([])


@pytest.mark.parametrize(
    "causes, expected",
    [
        ([cause("method_selection"), cause("data_source", "contaminated", source=POS_SAME)],
         {"redefine": 0, "collect": 1, "check": 1, "calculate": 1}),  # 데이터 수집이 통계기법 선택보다 앞선다
        ([cause("method_selection"), cause("assumption", "value_out_of_range", assumption_id=TREND_ID)],
         {"redefine": 1, "collect": 1, "check": 1, "calculate": 1}),  # 가정 정의가 가장 앞선다
        ([cause("method_selection"), cause("method_selection")],
         {"redefine": 0, "collect": 0, "check": 0, "calculate": 1}),
    ],
    ids=["data_source+method", "assumption+method", "method+method"],
)
def test_the_earliest_resume_point_among_the_causes_decides_where_to_start(step_calls, causes, expected):
    handling().rerun(causes)

    assert step_calls == expected


def test_causes_are_applied_in_step_order_whatever_order_they_arrive_in():
    outdated = cause("data_source", "outdated", source=MARKET, use_from=date(2013, 6, 1))
    forward = handling().rerun([outdated, cause("method_selection")])
    backward = handling().rerun([cause("method_selection"), outdated])

    for result in (forward, backward):
        market_source = next(s for s in result.collection.data_sources if s.kind == "market")
        assert market_source.use_from == date(2013, 6, 1)  # 데이터 수집 대응
        assert methods(result, DEFAULT_ID).isdisjoint(methods(first_run(), DEFAULT_ID))  # 통계기법 선택 대응
    assert [a.value for a in forward.assumptions] == [a.value for a in backward.assumptions]


def test_the_no_means_comparison_is_made_once_after_every_cause_is_applied():
    """`insufficient`만 받으면 입력이 바뀌지 않아 제외되지만(시장 데이터가 없는 55개월 인스턴스), 기법 제외가 함께 오면
    모든 이유를 적용한 뒤 입력이 바뀌었으므로 제외하지 않는다."""
    alone = handling("2013-01-01", with_market=False).rerun([cause("data_source", "insufficient")])
    together = handling("2013-01-01", with_market=False).rerun(
        [cause("data_source", "insufficient"), cause("method_selection")]
    )

    assert alone.assumptions == [] and dropped(alone) == {DEFAULT_ID: ["data_source:insufficient"]}
    assert ids(together) == [DEFAULT_ID] and together.dropped == []
    assert methods(together, DEFAULT_ID).isdisjoint(methods(first_run("2013-01-01", with_market=False), DEFAULT_ID))


def test_an_assumption_without_means_is_excluded_once_with_every_cause_as_a_reason():
    result = handling("2013-01-01", with_market=False).rerun(
        [cause("data_source", "contaminated", source=ORDERS), cause("data_source", "insufficient")]
    )

    assert dropped(result) == {DEFAULT_ID: ["data_source:contaminated", "data_source:insufficient"]}


def test_assumption_and_data_source_causes_together_drop_only_the_named_assumption():
    result = handling("2015-01-01").rerun(
        [cause("data_source", "insufficient"), cause("assumption", "value_out_of_range", assumption_id=TREND_ID)]
    )

    assert ids(result) == [DEFAULT_ID] and dropped(result) == {TREND_ID: ["assumption:value_out_of_range"]}
    assert len(result.collection.training_for(DEFAULT_ID)) > len(first_run("2015-01-01").collection.training_for(DEFAULT_ID))


def test_named_drivers_that_the_assumption_does_not_have_change_nothing_so_the_assumption_has_no_means():
    result = handling().rerun(cause("assumption", "no_evidence", assumption_id=TREND_ID, drivers=["event"]))

    assert result.dropped_drivers == []  # 지목한 원인이 없어 제외한 원인이 없다
    assert dropped(result) == {TREND_ID: ["assumption:no_evidence"]}
    assert "입력이 직전 재실행과 같아" in result.dropped[0].rationale
    assert ids(result) == [DEFAULT_ID]


def test_the_named_driver_is_excluded_and_recorded_with_the_suspected_cause():
    result = handling().rerun(cause("assumption", "no_evidence", assumption_id=TREND_ID, drivers=["category_trend"]))

    (driver,) = result.dropped_drivers
    assert (driver.driver, driver.assumption_ids, driver.reasons) == ("category_trend", [TREND_ID], ["assumption:no_evidence"])
    assert dropped(result) == {TREND_ID: ["assumption:no_evidence"]}  # 원인이 남지 않아 수단 없음


# --- 반드시 끝난다 ---------------------------------------------------------------------------------------


def test_repeated_causes_always_end_with_the_assumptions_excluded():
    for suspected in (cause("method_selection"), cause("data_source", "insufficient")):
        process = handling("2015-01-01")
        result = process.result
        steps = 0
        while result.assumptions and steps < 10:  # 반복할 때마다 입력이 바뀌거나 그 가정이 제외된다
            result = process.rerun(suspected)
            steps += 1

        assert result.assumptions == [] and steps < 10


# --- State 반영: 가정이 모두 제외됐을 때 options_exhausted와 no_computable_assumption을 가른다 ---------


def make_store() -> StateStore:
    record = ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")
    return make_pipeline_store([record])


def validate_and_send(store: StateStore, inputs: InstanceInputs):
    """검증agent가 판정하고 supply_coordination이 받아 만든 allocation_candidate 목록."""
    rules = forecast_rules(lambda company_id, item_id: monthly_order_history(inputs.orders, inputs.data_end))
    return validate_and_allocate(store, rules)


def record_of(store: StateStore) -> ForecastRecord:
    return store.get_field("forecast", "forecast_records[0]")


async def test_first_run_reflects_the_steps_in_state_and_sends_the_request():
    store = make_store()
    inputs = make()

    result, submitted = await run_forecast_steps_select_and_submit(store, "forecast", inputs)
    assert result is not None
    (candidate,) = validate_and_send(store, inputs)

    record = record_of(store)
    assert submitted and ids(result) == [a.assumption_id for a in record.assumptions]
    assert candidate.allocation == {AGENT_ID: record.scenario.value if record.scenario else None}
    assert record.scenario is not None and record.selection_basis == "rule"
    assert [s.kind for s in record.data_sources].count("orders") == 1 and record.cleaning == result.collection.cleaning
    assert record.validation is not None and record.validation.status == "passed"
    assert store.get_field("forecast", "escalation_records") == []


async def test_all_assumptions_excluded_after_a_scenario_existed_is_options_exhausted_and_keeps_scenario_and_assumptions():
    store = make_store()
    inputs = make("2013-01-01", with_market=False)
    result, submitted = await run_forecast_steps_select_and_submit(store, "forecast", inputs)
    assert result is not None
    assert len(validate_and_send(store, inputs)) == 1
    before = record_of(store)
    assert submitted and before.scenario is not None and before.assumptions

    sent = await rerun_forecast_steps_select_and_submit(
        store, "forecast", ForecastRerunHandling(inputs, result),
        cause("data_source", "insufficient"), "send_back",
    )

    after = record_of(store)
    assert sent is False
    assert after.scenario == before.scenario and after.selection_basis == before.selection_basis  # 재실행 전 값 그대로
    assert after.assumptions == before.assumptions  # scenario를 만든 가정 목록도 그대로(제외 기록에도 있을 수 있다)
    assert [(e.assumption_id, e.reasons) for e in after.excluded_assumptions] == [(DEFAULT_ID, ["data_source:insufficient"])]
    assert DEFAULT_ID in [a.assumption_id for a in after.assumptions]
    (escalation,) = store.get_field("forecast", "escalation_records")
    assert (escalation.reason, escalation.mode, escalation.status, escalation.agent_id) == (
        "options_exhausted", "intervention", "open", AGENT_ID,
    )
    assert "data_source:insufficient" in escalation.rationale
    assert store.queue("forecast_validation").empty()  # 새 요청량을 검증agent에 넘기지 않는다
    assert len(store.get_field("supply_coordination", "allocation_candidates")) == 1
    assert after.validation is None  # 재실행한 결과는 아직 검증받지 않았다
    (rerun_entry,) = [e for e in store.negotiation_log("forecast") if e.event == "rerun"]
    assert rerun_entry.agent_id == AGENT_ID and rerun_entry.payload["suspected_cause_reasons"] == ["data_source:insufficient"]


async def test_all_assumptions_excluded_without_an_earlier_scenario_is_no_computable_assumption(monkeypatch):
    from sop import forecast_method_selection as fms

    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", lambda *a, **k: float("nan"))
    store = make_store()
    result, submitted = await run_forecast_steps_select_and_submit(store, "forecast", make())
    assert result is not None
    assert submitted is False and record_of(store).scenario is None

    sent = await rerun_forecast_steps_select_and_submit(
        store, "forecast", ForecastRerunHandling(make(), result),
        cause("data_source", "insufficient"), "send_back",
    )

    after = record_of(store)
    assert sent is False and after.scenario is None and after.selection_basis is None
    (escalation,) = store.get_field("forecast", "escalation_records")  # 같은 reason의 미처리 기록이라 새로 만들지 않는다
    assert escalation.reason == "no_computable_assumption"


async def test_a_rerun_that_leaves_assumptions_replaces_the_scenario_and_sends_the_new_request():
    store = make_store()
    inputs = make()
    result, submitted = await run_forecast_steps_select_and_submit(store, "forecast", inputs)
    assert result is not None
    (first,) = validate_and_send(store, inputs)
    assert submitted

    sent = await rerun_forecast_steps_select_and_submit(
        store, "forecast", ForecastRerunHandling(inputs, result),
        cause("assumption", "value_out_of_range", assumption_id=TREND_ID), "send_back",
    )
    (second,) = validate_and_send(store, inputs)

    record = record_of(store)
    assert sent is True and second.plan_id != first.plan_id
    assert record.scenario is not None and record.scenario.assumption_ids == [DEFAULT_ID]
    assert [e.assumption_id for e in record.excluded_assumptions] == [TREND_ID]
    assert [(d.driver, d.reasons) for d in record.excluded_drivers] == [("category_trend", ["assumption:value_out_of_range"])]
    assert store.get_field("forecast", "escalation_records") == []
    assert len(store.get_field("supply_coordination", "allocation_candidates")) == 2


def test_every_step_and_the_rerun_judgments_use_the_common_judgment_schema():
    """공통 규칙 2: 규칙 기반 판단이 `{판단값, 근거}` 공통 모델로 반환된다(M7에서 LLM이 같은 스키마로 이어받는다)."""
    first = first_run()
    redefined = handling().rerun(cause("assumption", "value_out_of_range", assumption_id=TREND_ID))

    for result in (first, redefined):
        assert result.judgments and all(isinstance(j, StructuredJudgment) and j.reasoning for j in result.judgments)
    assert any(j.judgment.get("decision") == "assumption_redefined" for j in redefined.judgments)
