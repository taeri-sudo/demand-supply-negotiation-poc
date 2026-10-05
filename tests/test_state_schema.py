"""STATE_SCHEMA.md 확정 스키마(forecast_records 등)가 문서의 제약을 그대로 강제하는지 확인."""

from datetime import date

import pytest
from pydantic import ValidationError

from sop.state import (
    Driver,
    EscalationRecord,
    Evidence,
    ExcludedSource,
    ForecastRecord,
    InteractionProtocol,
    MethodValue,
    NoticeThreshold,
    Assumption,
    Scenario,
    SourceRef,
    State,
    SuspectedCause,
    ValidationResult,
)


def test_forecast_record_requires_company_id():
    """company_id는 필수이며 null을 허용하지 않는다."""
    with pytest.raises(ValidationError):
        ForecastRecord.model_validate({"agent_id": "X:RAMEN", "item_id": "RAMEN"})
    with pytest.raises(ValidationError):
        ForecastRecord.model_validate({"agent_id": "X:RAMEN", "company_id": None, "item_id": "RAMEN"})


def test_state_has_forecast_records_and_no_legacy_forecast_agents():
    state = State()
    assert state.forecast_records == []
    assert not hasattr(state, "forecast_agents")


def test_forecast_record_defaults_for_fields_filled_by_later_steps():
    record = ForecastRecord(agent_id="A:RAMEN", company_id="A", item_id="RAMEN")
    assert record.assumptions == []
    assert record.data_sources == []
    assert record.excluded_sources == []
    assert record.cleaning.applied is False and record.cleaning.count == 0
    assert record.premises == [] and record.excluded_drivers == [] and record.excluded_assumptions == []
    assert not hasattr(record, "forecast_method")  # 기법은 가정마다 고른다
    assert record.scenario is None
    assert record.validation is None


def test_default_assumption_has_no_drivers_and_assumption_with_two_drivers_has_one_value():
    """기본 가정은 driver가 비어 있고, driver가 여러 개인 가정도 value는 하나, 기법별 값은 가정 안에만 있다."""
    default = Assumption(assumption_id="A-DEFAULT")
    assert default.drivers == []
    assert default.defined_by == "rule"
    assert default.method_values == [] and default.value is None

    evidence = Evidence(kind="market", item_scope="category", refs=["D151"])
    combined = Assumption(
        assumption_id="A-2",
        drivers=[Driver(driver="category_trend", evidence=evidence), Driver(driver="event", evidence=evidence)],
        method_values=[
            MethodValue(method="regression", value=105.0, method_weight=0.6),
            MethodValue(method="regression_ar1", value=118.0, method_weight=0.4),
        ],
        value=110.0,
        occurrence_likelihood=None,
    )
    assert len(combined.drivers) == 2
    assert combined.value == 110.0 and combined.occurrence_likelihood is None


def test_schema_has_no_intermediate_effect_cost_or_assumption_likelihood_fields():
    """Driver에는 demand_effect가, 가정에는 cost_estimate와 likelihood 필드가 없고 발생 가능성은 occurrence_likelihood로만 둔다."""
    assert "demand_effect" not in Driver.model_fields
    assert "cost_estimate" not in Assumption.model_fields
    assert "likelihood" not in Assumption.model_fields
    assert "occurrence_likelihood" in Assumption.model_fields
    assert "method_weight" in MethodValue.model_fields  # 기법 가중치와 가정 발생 가능성은 이름이 다르다


def test_scenario_is_the_final_request_with_its_source_assumptions():
    scenario = Scenario(value=110.0, assumption_ids=["A-DEFAULT", "A-TREND"], derivation="mean")
    record = ForecastRecord(agent_id="A:R", company_id="A", item_id="R", scenario=scenario)
    assert record.scenario is not None and record.scenario.assumption_ids == ["A-DEFAULT", "A-TREND"]
    with pytest.raises(ValidationError):
        Scenario.model_validate({"value": 1.0, "assumption_ids": [], "derivation": "guess"})


def test_driver_rejects_free_text():
    """수요 동인은 데이터가 존재하는 세 값만 허용한다(자유 텍스트 금지)."""
    with pytest.raises(ValidationError):
        Driver.model_validate({"driver": "weather", "evidence": {"kind": "pos", "item_scope": "same_item"}})


def test_kind_and_item_scope_are_independent_and_all_nine_combinations_are_valid():
    for kind in ("orders", "pos", "market"):
        for scope in ("same_item", "similar_item", "category"):
            Evidence(kind=kind, item_scope=scope)


def test_excluded_source_reason_is_irrelevant_only():
    assert ExcludedSource(kind="market", item_scope="category").reason == "irrelevant"
    with pytest.raises(ValidationError):
        ExcludedSource.model_validate({"kind": "market", "item_scope": "category", "reason": "contaminated"})


@pytest.mark.parametrize(
    "issue", ["no_evidence", "value_out_of_range", "not_distinct", "double_counted"]
)
def test_suspected_cause_assumption_accepts_only_assumption_issues(issue):
    cause = SuspectedCause(type="assumption", issue=issue, assumption_id="A-1")
    assert cause.issue == issue


@pytest.mark.parametrize("issue", ["insufficient", "contaminated", "irrelevant"])
def test_suspected_cause_data_source_accepts_only_data_source_issues(issue):
    cause = SuspectedCause(
        type="data_source",
        issue=issue,
        source=SourceRef(kind="orders", item_scope="same_item"),
        use_from=date(2016, 1, 1) if issue == "irrelevant" else None,
    )
    assert cause.issue == issue
    assert cause.source is not None
    assert cause.source.kind == "orders"


def test_suspected_cause_method_selection_has_no_issue():
    assert SuspectedCause(type="method_selection").issue is None
    with pytest.raises(ValidationError):
        SuspectedCause(type="method_selection", issue="insufficient")


def test_suspected_cause_rejects_issue_of_the_other_type():
    with pytest.raises(ValidationError):
        SuspectedCause(type="assumption", issue="contaminated")
    with pytest.raises(ValidationError):
        SuspectedCause(type="data_source", issue="not_distinct")
    with pytest.raises(ValidationError):
        SuspectedCause(type="assumption")  # issue 누락


def test_validation_result_carries_structured_suspected_cause():
    result = ValidationResult(
        status="flagged",
        suspected_cause=SuspectedCause(type="assumption", issue="not_distinct", assumption_id="A-2"),
        validator_role_tag="forecast_validation",
    )
    assert result.suspected_cause is not None
    assert result.suspected_cause.assumption_id == "A-2"


def test_supply_coordination_to_human_manager_notice_protocol_entry():
    """알림용 interaction_protocol 항목 — 구속력 없는 약정이 더 작은 차이에도 알린다."""
    entry = InteractionProtocol(
        edge="supply_coordination->human_manager",
        scope=["supply_coordination", "human_manager"],
        escalation_trigger="commitment_gap",
        escalation_target="human_manager",
        escalation_kind="rule",
        escalation_mode="notice",
        notice_threshold=NoticeThreshold(binding=0.2, non_binding=0.05),
    )
    assert entry.max_rounds is None
    assert entry.notice_threshold is not None
    assert entry.notice_threshold.non_binding < entry.notice_threshold.binding


def test_escalation_record_mode_distinguishes_notice_from_intervention():
    notice = EscalationRecord(
        trigger_edge="supply_coordination->human_manager", reason="commitment_gap", mode="notice", status="sent"
    )
    assert notice.resolution is None
    assert EscalationRecord(trigger_edge="e", reason="r", status="open").mode == "intervention"
    with pytest.raises(ValidationError):
        EscalationRecord.model_validate({"trigger_edge": "e", "reason": "r", "mode": "silent", "status": "open"})
