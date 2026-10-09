"""가정 정의 → 데이터 수집 → 원인 확인 → 통계기법 선택·가정별 요청량 예측값 계산 테스트.

[테스트 전용 입력] 시장 지수와 POS 수준은 로직 검증을 위해 시장 변화율과 우리 수요 변화율의 관계를
직접 정해 만든 것이다(실데이터가 아니다). `linked_market_and_pos`는 연동 근거가 있는 경우,
`unlinked_market_and_pos`는 없는 경우다.
"""

import inspect
from functools import cache

import numpy as np
import pandas as pd
import pytest

from sop import forecast_assumption_definition as definition_module
from sop import forecast_assumption_selection, state
from sop.data_source_judgment import collect_instance_data, last_complete_month
from sop.external_data import InstanceInputs
from sop.forecast_assumption_calc import combine_method_values, select_methods_and_calculate_values
from sop.forecast_assumption_definition import DEFAULT_ID, TREND_ID, define_assumptions
from sop.forecast_driver_check import check_drivers
from sop.judgment_thresholds import (
    ASSUMPTION_METHOD_SPREAD,
    MAX_ASSUMPTIONS_FOR_SELECTION,
    MAX_DRIVERS_PER_ASSUMPTION,
    MAX_METHODS_PER_ASSUMPTION,
    METHOD_DOMINANT_WEIGHT,
    TREND_LAG_MONTHS,
)
from sop.stats_adapter import REGRESSION_METHODS
from sop.state import ForecastRecord, MethodValue
from test_data_source_judgment import END, empty_similar, orders_from_pos

MONTHS = pd.date_range("2012-01-01", "2017-12-01", freq="MS")  # 시장은 2017-12까지 공표돼 있다
N = len(MONTHS)
SEASON = 1 + 0.2 * np.sin(2 * np.pi * np.arange(N) / 12)
PROMO_MONTHS = (pd.Timestamp("2014-11-01"), pd.Timestamp("2015-11-01"), pd.Timestamp("2016-11-01"))


def _levels(log_changes, start):
    return pd.Series(start * np.exp(np.cumsum(log_changes)) * SEASON, index=MONTHS)


def _market_and_pos(beta, seed):
    """[테스트 전용] 우리 수요 변화율 = beta × (TREND_LAG_MONTHS개월 전 시장 변화율) + 잡음."""
    rng = np.random.default_rng(seed)
    market_change = rng.normal(0.0, 0.03, N)
    demand_change = rng.normal(0.0, 0.01, N)
    demand_change[TREND_LAG_MONTHS:] += beta * market_change[:-TREND_LAG_MONTHS]
    market = _levels(market_change, 100.0)
    monthly = _levels(demand_change, 1500.0)
    days = pd.date_range("2013-01-01", END, freq="D")
    month = days.to_period("M").to_timestamp()
    pos = pd.DataFrame(
        {
            "date": days,
            "quantity": monthly.reindex(month).to_numpy() / days.days_in_month,
            "promotion": pd.array([False] * len(days), dtype="boolean"),
        }
    )
    return market, pos


def make_inputs(beta, seed, with_market=True, promo=False):
    market, pos = _market_and_pos(beta, seed)
    months = set(PROMO_MONTHS) if promo else set()
    orders = orders_from_pos(
        pos, "2013-01-01", first_factor=1.0, promo_months=months, spikes={m: 1.5 for m in months}
    )
    return InstanceInputs(
        company_id="CUST-01",
        item_id="ITEM-1",
        family="DAIRY",
        industry="food_processing",
        perishable=False,
        data_end=END,
        orders=orders,
        pos_same=pos,
        pos_similar=empty_similar(),
        pos_category=pd.DataFrame({"month": [], "quantity": []}),
        market_index=market if with_market else None,
        market_group_index=market.to_frame("D152") if with_market else None,
    )


def run_pipeline(inputs, scheduled_promotion=False):
    """가정 정의(맨 앞) → 데이터 수집 → 원인 확인 → 통계기법 선택과 가정별 요청량 예측값 계산."""
    definition = define_assumptions(inputs, scheduled_promotion)
    collection = collect_instance_data(inputs, definition.required_evidence)
    planning = last_complete_month(inputs.data_end)
    check = check_drivers(definition.assumptions, definition.premises, inputs, collection, planning)
    calculated = select_methods_and_calculate_values(check.assumptions, check.premises, inputs, collection, planning)
    return definition, collection, check, calculated


@cache
def linked():  # 연동 근거가 있는 입력
    return run_pipeline(make_inputs(1.0, 3))


@cache
def unlinked():
    return run_pipeline(make_inputs(0.0, 0))


@cache
def no_market():
    return run_pipeline(make_inputs(1.0, 3, with_market=False))


@cache
def promo_with_history():
    return run_pipeline(make_inputs(1.0, 3, promo=True), scheduled_promotion=True)


@cache
def promo_without_history():
    return run_pipeline(make_inputs(1.0, 3), scheduled_promotion=True)


def ids(assumptions):
    return [a.assumption_id for a in assumptions]


def by_id(calculated, assumption_id):
    return next(a for a in calculated.assumptions if a.assumption_id == assumption_id)


# --- 가정 정의: 가정과 근거를 선언만 한다 ---------------------------------------------------------


def test_definition_only_declares_assumptions_premises_and_required_evidence():
    definition, *_ = promo_with_history()

    assert ids(definition.assumptions) == [DEFAULT_ID, TREND_ID]
    assert definition.required_evidence == [("pos", "same_item"), ("market", "category"), ("orders", "same_item")]
    trend = definition.assumptions[1].drivers[0]
    assert trend.driver == "category_trend" and trend.evidence.kind == "market"
    assert definition.assumptions[0].drivers == []  # 기본 가정
    # 확정된 프로모션 일정은 가정의 요소가 아니라 모든 가정의 전제다
    assert [p.driver for p in definition.premises] == ["event"]
    assert all(d.driver != "event" for a in definition.assumptions for d in a.drivers)
    # 기법별 값과 value는 후속 단계가 채운다
    assert all(a.method_values == [] and a.value is None for a in definition.assumptions)
    assert all(a.defined_by == "rule" for a in definition.assumptions)


def test_no_premise_or_orders_evidence_is_declared_without_a_scheduled_promotion():
    definition, *_ = linked()
    assert definition.premises == []
    assert ("orders", "same_item") not in definition.required_evidence


def test_the_three_limits_are_constants_with_initial_values():
    assert (MAX_DRIVERS_PER_ASSUMPTION, MAX_ASSUMPTIONS_FOR_SELECTION, MAX_METHODS_PER_ASSUMPTION) == (5, 20, 5)


def test_definition_respects_the_limits(monkeypatch):
    inputs = make_inputs(1.0, 3)
    monkeypatch.setattr(definition_module, "MAX_ASSUMPTIONS_FOR_SELECTION", 1)
    assert ids(define_assumptions(inputs).assumptions) == [DEFAULT_ID]
    monkeypatch.setattr(definition_module, "MAX_DRIVERS_PER_ASSUMPTION", 0)
    with pytest.raises(ValueError):
        define_assumptions(inputs)


# --- 원인 확인: 연동 근거에 따라 category_trend 원인을 단 가정이 남거나 제외된다 -----------------


def test_linked_evidence_keeps_the_category_trend_assumption():
    _, _, check, calculated = linked()

    assert ids(check.assumptions) == [DEFAULT_ID, TREND_ID] and check.excluded_drivers == []
    assert ids(calculated.assumptions) == [DEFAULT_ID, TREND_ID]


def test_ci_containing_zero_excludes_the_assumption_and_records_the_driver_and_affected_assumptions():
    _, _, check, calculated = unlinked()

    assert ids(calculated.assumptions) == [DEFAULT_ID]
    (record,) = check.excluded_drivers
    assert record.driver == "category_trend" and record.assumption_ids == [TREND_ID]
    assert record.reasons == ["no_significant_effect"] and record.rationale
    assert any(j.judgment["decision"] == "no_significant_effect" for j in check.judgments)
    stored = ForecastRecord(
        agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1", excluded_drivers=check.excluded_drivers
    )
    assert stored.excluded_drivers[0].assumption_ids == [TREND_ID]


def test_unavailable_market_evidence_is_reported_and_excludes_the_assumption():
    _, collection, check, calculated = no_market()

    assert collection.evidence_status[("market", "category")] == "unavailable"
    assert collection.evidence_status[("pos", "same_item")] == "collected"
    assert ids(calculated.assumptions) == [DEFAULT_ID]
    assert check.excluded_drivers[0].reasons == ["no_evidence"]


def test_collected_evidence_is_recorded_in_data_sources_but_not_requested_evidence_is_not():
    inputs = make_inputs(1.0, 3)
    with_request = collect_instance_data(inputs, define_assumptions(inputs).required_evidence)
    without_request = collect_instance_data(inputs)

    assert ("market", "category") in {(s.kind, s.item_scope) for s in with_request.data_sources}
    assert ("market", "category") not in {(s.kind, s.item_scope) for s in without_request.data_sources}
    assert without_request.evidence_status == {}


def test_evidence_the_data_cannot_provide_is_unavailable_not_an_error():
    collection = collect_instance_data(make_inputs(1.0, 3, with_market=False), [("market", "same_item"), ("orders", "category")])
    assert collection.evidence_status == {
        ("market", "same_item"): "unavailable",
        ("orders", "category"): "unavailable",
    }


# --- 확정된 프로모션 일정(전제): 기록이 부족하면 빠지고 이유가 남는다 -------------------------------


def test_premise_with_enough_records_applies_to_every_assumption():
    _, _, check, calculated = promo_with_history()

    assert [p.driver for p in check.premises] == ["event"] and check.excluded_drivers == []
    for assumption in calculated.assumptions:
        # 전제는 설명변수로 모든 가정에 들어가므로 반영할 수 있는 회귀 계열 기법만 남는다
        assert {m.method for m in assumption.method_values} <= set(REGRESSION_METHODS)
    selections = [j for j in calculated.judgments if "regressors" in j.judgment]
    assert all("event" in j.judgment["regressors"] for j in selections)


def test_premise_without_records_is_dropped_from_every_assumption_and_the_reason_is_recorded():
    _, _, check, calculated = promo_without_history()

    assert check.premises == []
    (record,) = [r for r in check.excluded_drivers if r.driver == "event"]
    assert record.reasons == ["no_evidence"] and set(record.assumption_ids) == {DEFAULT_ID, TREND_ID}
    assert record.rationale
    assert ids(calculated.assumptions) == [DEFAULT_ID, TREND_ID]  # 가정은 제외되지 않는다


# --- 가정별 기법별 값 -------------------------------------------------------------------------------


def test_default_assumption_values_come_from_the_order_history_only():
    """기본 가정의 기법별 값은 시장 데이터가 있든 없든 같다(우리 주문 이력만으로 계산)."""
    with_market = by_id(linked()[3], DEFAULT_ID)
    without_market = by_id(no_market()[3], DEFAULT_ID)

    assert [(m.method, round(m.value, 6)) for m in with_market.method_values] == [
        (m.method, round(m.value, 6)) for m in without_market.method_values
    ]
    assert with_market.drivers == []


def test_each_assumption_has_its_own_methods_weights_and_a_single_value():
    calculated = linked()[3]
    default, trend = by_id(calculated, DEFAULT_ID), by_id(calculated, TREND_ID)

    assert {m.method for m in trend.method_values} <= set(REGRESSION_METHODS)  # driver를 반영할 수 있는 기법만
    assert len(default.method_values) <= MAX_METHODS_PER_ASSUMPTION
    for assumption in (default, trend):
        assert sum(m.method_weight for m in assumption.method_values) == pytest.approx(1.0)
        values = [m.value for m in assumption.method_values]
        assert min(values) - 1e-9 <= (assumption.value or 0.0) <= max(values) + 1e-9
        assert assumption.occurrence_likelihood is None  # 발생 가능성의 근거는 M2에서 정하지 않는다
    # 같은 데이터라도 가정마다 따로 잰 정확도로 기법 가중치가 정해진다
    assert [(m.method, round(m.method_weight, 4)) for m in default.method_values] != [
        (m.method, round(m.method_weight, 4)) for m in trend.method_values
    ]


def test_assumption_without_any_applicable_method_is_excluded_with_the_reason(monkeypatch):
    from sop import forecast_method_selection as fms

    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", lambda *a, **k: float("nan"))
    inputs = make_inputs(1.0, 3)
    definition = define_assumptions(inputs)
    collection = collect_instance_data(inputs, definition.required_evidence)
    planning = last_complete_month(inputs.data_end)
    check = check_drivers(definition.assumptions, definition.premises, inputs, collection, planning)

    calculated = select_methods_and_calculate_values(check.assumptions, check.premises, inputs, collection, planning)

    assert calculated.assumptions == []
    assert {e.assumption_id for e in calculated.excluded_assumptions} == {DEFAULT_ID, TREND_ID}
    assert all(e.reasons == ["no_applicable_method"] and e.rationale for e in calculated.excluded_assumptions)


# --- 가정 안: 기법별 값을 합치거나 하나 선택 ----------------------------------------------------------


def values(*pairs):
    return [MethodValue(method=f"m{i}", value=v, method_weight=w) for i, (v, w) in enumerate(pairs)]


def test_a_single_method_is_used_as_is():
    result = combine_method_values(values((120.0, 1.0)))
    assert result.judgment["value"] == 120.0 and result.judgment["how"] == "single"


def test_a_dominant_method_is_selected_and_otherwise_weights_are_averaged():
    assert METHOD_DOMINANT_WEIGHT == 0.7
    dominant = combine_method_values(values((100.0, 0.7), (110.0, 0.3)))  # 경계 0.7 포함
    assert dominant.judgment["how"] == "selected" and dominant.judgment["value"] == 100.0

    mixed = combine_method_values(values((100.0, 0.6), (110.0, 0.4)))
    assert mixed.judgment["how"] == "weighted_mean"
    assert mixed.judgment["value"] == pytest.approx(0.6 * 100 + 0.4 * 110)


def test_widely_scattered_method_values_are_marked_ambiguous():
    assert ASSUMPTION_METHOD_SPREAD == 0.5
    scattered = combine_method_values(values((50.0, 0.5), (150.0, 0.5)))
    assert scattered.ambiguous and scattered.ambiguity_reason
    assert not combine_method_values(values((100.0, 0.5), (110.0, 0.5))).ambiguous


# --- 최소 구매 약정은 forecast의 입력이 아니다 --------------------------------------------------


def test_forecast_has_no_minimum_purchase_input_or_commitment_assumption():
    assert "minimum_commitment" not in str(state.DriverName)
    assert "minimum_purchase" not in inspect.signature(define_assumptions).parameters
    assert "minimum_purchase" not in inspect.signature(forecast_assumption_selection.select_forecast_assumption).parameters
    definition, *_ = linked()
    assert all("COMMITMENT" not in a.assumption_id for a in definition.assumptions)


# --- 기본 가정은 전제 유무와 상관없이 항상 후보에 있다 ----------------------------------------------


def _run_with_patched_evaluation(monkeypatch, fail):
    """[테스트 전용] `fail(method, regressors)`가 참인 평가는 계산 불가(NaN)로 만든다."""
    from sop import forecast_method_selection as fms

    real = fms.stats_adapter.walk_forward_mae

    def patched(method, y, origins, regressors=None):
        return float("nan") if fail(method, regressors) else real(method, y, origins, regressors)

    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", patched)
    return run_pipeline(make_inputs(1.0, 3, promo=True), scheduled_promotion=True)


def test_default_assumption_falls_back_to_the_order_history_when_no_method_can_reflect_the_premise(monkeypatch):
    _, _, check, calculated = _run_with_patched_evaluation(
        monkeypatch, lambda method, regressors: method in REGRESSION_METHODS and regressors is not None
    )

    default = by_id(calculated, DEFAULT_ID)  # 기본 가정이 빠지지 않는다
    assert default.value is not None and default.drivers == []
    assert {m.method for m in default.method_values} - set(REGRESSION_METHODS)  # 시계열 기법이 다시 후보가 된다
    retried = [j for j in calculated.judgments if j.judgment.get("assumption_id") == DEFAULT_ID and "regressors" in j.judgment]
    assert retried[0].judgment["regressors"] == ["event"] and retried[-1].judgment["regressors"] == []  # 전제 없이 다시 계산
    (record,) = calculated.excluded_drivers
    assert record.driver == "event" and record.assumption_ids == [DEFAULT_ID]
    assert record.reasons == ["no_applicable_method"] and "전제" in record.rationale
    marks = [j for j in calculated.judgments if j.judgment.get("decision") == "premise_not_reflected"]
    assert len(marks) == 1 and marks[0].ambiguous and marks[0].ambiguity_reason
    assert marks[0].judgment["assumption_id"] == DEFAULT_ID
    assert ids(calculated.assumptions) == [DEFAULT_ID]  # 원인이 있는 가정은 맞는 기법이 없으면 제외
    assert [e.assumption_id for e in calculated.excluded_assumptions] == [TREND_ID]
    stored = ForecastRecord(
        agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1",
        excluded_drivers=[*check.excluded_drivers, *calculated.excluded_drivers],
    )
    assert stored.excluded_drivers[0].reasons == ["no_applicable_method"]


def test_default_assumption_without_a_reflected_premise_is_not_marked_when_the_premise_is_reflected():
    _, _, _, calculated = promo_with_history()
    assert calculated.excluded_drivers == []
    assert not any(j.judgment.get("decision") == "premise_not_reflected" for j in calculated.judgments)


def test_default_assumption_is_excluded_only_when_even_without_the_premise_no_method_can_be_computed(monkeypatch):
    _, _, _, calculated = _run_with_patched_evaluation(monkeypatch, lambda method, regressors: True)

    assert calculated.assumptions == []
    assert DEFAULT_ID in {e.assumption_id for e in calculated.excluded_assumptions}
    assert calculated.excluded_drivers == []  # 전제 없이도 실패했으므로 "전제만 못 반영"이 아니다
