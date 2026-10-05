"""가정 선택 — 하나를 택하거나, 값이 너무 갈리면 평균·중간값을 내거나, 가정이 하나면 그대로 전달한다."""

import pytest

from sop.forecast_assumption_selection import escalation_records_for, select_forecast_assumption
from sop.judgment import StructuredJudgment
from sop.judgment_thresholds import (
    ASSUMPTION_CLEAR_LEADER_RATIO,
    ASSUMPTION_CLOSE_REL_RANGE,
    ASSUMPTION_SPLIT_REL_RANGE,
    MAX_ASSUMPTIONS_FOR_SELECTION,
)
from sop.state import Assumption


def assumption(assumption_id, value, uncertainty=None, occurrence=None):
    return Assumption(
        assumption_id=assumption_id, value=value, forecast_uncertainty=uncertainty, occurrence_likelihood=occurrence
    )


def scenario(result):
    return result.judgment["scenario"]


def test_one_assumption_is_passed_through_unchanged():
    result = select_forecast_assumption([assumption("a", 100.0)])

    assert isinstance(result, StructuredJudgment)  # 공통 규칙 2: M7에서 LLM이 같은 스키마로 이어받는다
    assert scenario(result) == {"value": 100.0, "assumption_ids": ["a"], "derivation": "pass_through"}
    assert not result.ambiguous and not result.judgment["escalate"]


def test_nearly_equal_values_are_averaged():
    assert ASSUMPTION_CLOSE_REL_RANGE == 0.05
    result = select_forecast_assumption([assumption("a", 100.0, 5.0), assumption("b", 105.0, 5.0)])  # 상대 범위 5%(경계 포함)
    assert scenario(result)["derivation"] == "mean"
    assert scenario(result)["value"] == pytest.approx(102.5)
    assert scenario(result)["assumption_ids"] == ["a", "b"]


def test_a_clearly_more_plausible_assumption_is_chosen_by_its_past_accuracy():
    # 상대 오차 a=0.05, b=0.20 → 점수(오차 역제곱) 400 대 25
    result = select_forecast_assumption([assumption("a", 100.0, 5.0), assumption("b", 112.0, 22.4)])

    assert scenario(result) == {"value": 100.0, "assumption_ids": ["a"], "derivation": "chosen"}
    assert not result.ambiguous  # 상대 범위가 크게 갈리는 수준(20%)이 아님


def test_chosen_but_other_assumptions_strongly_disagree_is_marked_ambiguous():
    result = select_forecast_assumption([assumption("a", 100.0, 5.0), assumption("b", 150.0, 30.0)])
    assert scenario(result)["derivation"] == "chosen" and scenario(result)["assumption_ids"] == ["a"]
    assert result.ambiguous and result.ambiguity_reason
    assert not result.judgment["escalate"]  # 뚜렷한 가정이 있으므로 사람까지 올리지 않는다


def test_the_occurrence_likelihood_is_used_as_the_score_when_every_assumption_has_it():
    result = select_forecast_assumption(
        [assumption("a", 100.0, 5.0, occurrence=0.2), assumption("b", 112.0, 22.4, occurrence=0.8)]
    )
    assert scenario(result)["assumption_ids"] == ["b"] and scenario(result)["derivation"] == "chosen"


def test_no_clear_leader_and_moderate_range_averages_and_marks_ambiguous():
    assert ASSUMPTION_CLEAR_LEADER_RATIO == 1.5
    result = select_forecast_assumption([assumption("a", 100.0, 10.0), assumption("b", 110.0, 11.0)])
    assert scenario(result)["derivation"] == "mean"
    assert scenario(result)["value"] == pytest.approx(105.0)
    assert result.ambiguous and not result.judgment["escalate"]


def test_scores_that_cannot_be_computed_leave_no_clear_leader():
    result = select_forecast_assumption([assumption("a", 100.0), assumption("b", 110.0)])  # 정확도 정보 없음
    assert scenario(result)["derivation"] == "mean" and result.ambiguous


def test_values_too_far_apart_without_a_clear_leader_use_the_median_and_escalate():
    assert ASSUMPTION_SPLIT_REL_RANGE == 0.20
    result = select_forecast_assumption(
        [assumption("a", 100.0, 10.0), assumption("b", 110.0, 11.0), assumption("c", 125.0, 12.5)]  # 상대 범위 25%/중앙값
    )
    assert scenario(result)["derivation"] == "median" and scenario(result)["value"] == 110.0
    assert result.ambiguous and result.judgment["escalate"]


def test_split_boundary_is_inclusive_at_twenty_percent():
    def three(top):  # 상대 오차가 모두 10%라 뚜렷한 가정이 없다. 상대 범위 = (top - 100) / 110
        return [assumption("a", 100.0, 10.0), assumption("b", 110.0, 11.0), assumption("c", top, top / 10)]

    at_boundary = select_forecast_assumption(three(122.0))
    below = select_forecast_assumption(three(121.0))
    assert at_boundary.judgment["escalate"] and scenario(at_boundary)["derivation"] == "median"
    assert not below.judgment["escalate"] and scenario(below)["derivation"] == "mean"


def test_escalation_is_returned_as_an_intervention_record():
    split = select_forecast_assumption([assumption("a", 100.0, 10.0), assumption("b", 125.0, 12.5)])
    records = escalation_records_for(split)
    assert [r.mode for r in records] == ["intervention"]
    assert records[0].status == "pending"
    assert escalation_records_for(select_forecast_assumption([assumption("a", 1.0)])) == []


def test_costs_do_not_enter_the_choice():
    """cost_estimate가 없는 가정으로만 고르며, 비용으로 고르지 않는다."""
    assert "cost_estimate" not in Assumption.model_fields


def test_assumptions_beyond_the_limit_are_dropped_by_relative_error():
    many = [assumption(f"a{i}", 100.0 + i * 0.01, uncertainty=1.0 + i) for i in range(MAX_ASSUMPTIONS_FOR_SELECTION + 3)]
    result = select_forecast_assumption(many)
    assert len(scenario(result)["assumption_ids"]) <= MAX_ASSUMPTIONS_FOR_SELECTION
    assert "a22" not in scenario(result)["assumption_ids"]


def test_assumptions_not_yet_calculated_are_rejected():
    with pytest.raises(ValueError):
        select_forecast_assumption([Assumption(assumption_id="a")])
    with pytest.raises(ValueError):
        select_forecast_assumption([])
