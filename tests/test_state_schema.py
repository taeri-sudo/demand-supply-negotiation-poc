"""STATE_SCHEMA.md 확정 스키마(forecast_records 등)가 문서의 제약을 그대로 강제하는지 확인."""

from datetime import date

import pytest
from pydantic import ValidationError

from sop.state import (
    Assumption,
    EscalationRecord,
    Evidence,
    ExcludedSource,
    ForecastRecord,
    InteractionProtocol,
    NoticeThreshold,
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
    assert record.scenarios == []
    assert record.data_sources == []
    assert record.excluded_sources == []
    assert record.cleaning.applied is False and record.cleaning.count == 0
    assert record.forecast_method is None
    assert record.selected_scenario is None
    assert record.validation is None


def test_base_scenario_has_no_assumptions_and_scenario_with_two_assumptions_has_one_value():
    """기준 시나리오는 가정이 비어 있고, 가정이 여러 개인 시나리오도 value/cost_estimate는 하나씩."""
    base = Scenario(scenario_id="S-BASE")
    assert base.assumptions == []
    assert base.defined_by == "rule"

    evidence = Evidence(kind="market", item_scope="category", refs=["D151"])
    combined = Scenario(
        scenario_id="S-2",
        assumptions=[
            Assumption(driver="category_trend", demand_effect=-0.05, evidence=evidence),
            Assumption(driver="event", demand_effect=0.2, evidence=evidence),
        ],
        value=110.0,
        cost_estimate=42.0,
    )
    assert len(combined.assumptions) == 2
    assert combined.value == 110.0 and combined.cost_estimate == 42.0


def test_driver_rejects_free_text():
    """수요 동인은 데이터가 존재하는 세 값만 허용한다(자유 텍스트 금지)."""
    with pytest.raises(ValidationError):
        Assumption.model_validate(
            {
                "driver": "weather",
                "demand_effect": 0.1,
                "evidence": {"kind": "pos", "item_scope": "same_item"},
            }
        )


def test_kind_and_item_scope_are_independent_and_all_nine_combinations_are_valid():
    for kind in ("orders", "pos", "market"):
        for scope in ("same_item", "similar_item", "category"):
            Evidence(kind=kind, item_scope=scope)


def test_excluded_source_reason_is_irrelevant_only():
    assert ExcludedSource(kind="market", item_scope="category").reason == "irrelevant"
    with pytest.raises(ValidationError):
        ExcludedSource.model_validate({"kind": "market", "item_scope": "category", "reason": "contaminated"})


@pytest.mark.parametrize(
    "issue", ["no_evidence", "effect_out_of_range", "not_distinct", "double_counted"]
)
def test_suspected_cause_scenario_accepts_only_scenario_issues(issue):
    cause = SuspectedCause(type="scenario", issue=issue, scenario_id="S-1")
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


def test_suspected_cause_forecast_method_has_no_issue():
    assert SuspectedCause(type="forecast_method").issue is None
    with pytest.raises(ValidationError):
        SuspectedCause(type="forecast_method", issue="insufficient")


def test_suspected_cause_rejects_issue_of_the_other_type():
    with pytest.raises(ValidationError):
        SuspectedCause(type="scenario", issue="contaminated")
    with pytest.raises(ValidationError):
        SuspectedCause(type="data_source", issue="not_distinct")
    with pytest.raises(ValidationError):
        SuspectedCause(type="scenario")  # issue 누락


def test_validation_result_carries_structured_suspected_cause():
    result = ValidationResult(
        status="flagged",
        suspected_cause=SuspectedCause(type="scenario", issue="not_distinct", scenario_id="S-2"),
        validator_role_tag="forecast_validation",
    )
    assert result.suspected_cause is not None
    assert result.suspected_cause.scenario_id == "S-2"


def test_forecast_to_human_manager_notice_protocol_entry():
    """알림용 interaction_protocol 항목 — 구속력 없는 약정이 더 작은 차이에도 알린다."""
    entry = InteractionProtocol(
        edge="forecast->human_manager",
        scope=["forecast", "human_manager"],
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
        trigger_edge="forecast->human_manager", reason="commitment_gap", mode="notice", status="sent"
    )
    assert notice.resolution is None
    assert EscalationRecord(trigger_edge="e", reason="r", status="open").mode == "intervention"
    with pytest.raises(ValidationError):
        EscalationRecord.model_validate({"trigger_edge": "e", "reason": "r", "mode": "silent", "status": "open"})
