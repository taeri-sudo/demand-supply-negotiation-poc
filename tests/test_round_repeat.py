"""라운드 누적형 엣지의 같은 의심되는 원인의 연속 횟수: 합성한 `exchanges`와 더미 엣지로 확인한다.

[테스트 전용 입력] 실물 라운드 누적형 엣지는 M5에서 생기므로 교환 기록은 테스트가 직접 만든다.
"""

import sop.round_repeat as round_repeat
from sop.round_repeat import consecutive_repeats, exchange_reason, repeat_escalation_reason
from sop.state import Exchange, InteractionProtocol, SuspectedCause, ValidationResult

DUMMY_EDGE = InteractionProtocol(
    edge="dummy_hub<->dummy_plan", max_rounds=5, repeat_escalation_threshold=2, scope=["dummy_hub", "dummy_plan"]
)


def exchange(round_, reason=None, role="dummy_plan", failed_cause=None) -> Exchange:
    validation = (
        ValidationResult(status="failed", suspected_causes=[failed_cause]) if failed_cause is not None else None
    )
    return Exchange(
        role_tag=role, round=round_, request={}, response_status="infeasible",
        routing_reason=reason, validation=validation,
    )


def test_the_same_reason_twice_in_a_row_trips_before_max_rounds_is_used_up():
    exchanges = [exchange(1, "capacity_short"), exchange(2, "capacity_short")]

    assert repeat_escalation_reason([DUMMY_EDGE], "dummy_hub", "dummy_plan", exchanges) == "capacity_short"
    assert len(exchanges) < (DUMMY_EDGE.max_rounds or 0)  # max_rounds 소진 경로와 다르다


def test_alternating_reasons_do_not_trip():
    exchanges = [exchange(1, "capacity_short"), exchange(2, "late"), exchange(3, "capacity_short")]

    assert consecutive_repeats(exchanges) == ("capacity_short", 1)
    assert repeat_escalation_reason([DUMMY_EDGE], "dummy_hub", "dummy_plan", exchanges) is None


def test_a_different_reason_resets_the_count_and_nothing_is_stored():
    exchanges = [exchange(1, "capacity_short"), exchange(2, "capacity_short"), exchange(3, "late")]

    assert consecutive_repeats(exchanges) == ("late", 1)  # 연속이 끊기면 처음부터 센다
    assert repeat_escalation_reason([DUMMY_EDGE], "dummy_hub", "dummy_plan", exchanges) is None


def test_only_the_exchanges_of_that_edge_are_counted():
    exchanges = [
        exchange(1, "late", role="other_plan"), exchange(2, "late", role="other_plan"), exchange(3, "late"),
    ]

    assert repeat_escalation_reason([DUMMY_EDGE], "dummy_hub", "dummy_plan", exchanges) is None  # 새 엣지에서 새로 센다


def test_a_failed_validation_cause_counts_as_the_reason_when_there_is_no_routing_reason():
    cause = SuspectedCause(type="method_selection")
    exchanges = [exchange(1, failed_cause=cause), exchange(2, failed_cause=cause)]

    assert exchange_reason(exchanges[0]) == "method_selection"
    assert repeat_escalation_reason([DUMMY_EDGE], "dummy_hub", "dummy_plan", exchanges) == "method_selection"


def test_an_exchange_without_a_reason_ends_the_streak():
    assert consecutive_repeats([exchange(1, "late"), exchange(2)]) == (None, 0)
    assert consecutive_repeats([]) == (None, 0)


def test_an_edge_without_an_entry_or_threshold_never_trips():
    exchanges = [exchange(1, "late"), exchange(2, "late"), exchange(3, "late")]
    no_threshold = InteractionProtocol(edge="dummy_hub<->dummy_plan", scope=["dummy_hub", "dummy_plan"])

    assert repeat_escalation_reason([], "dummy_hub", "dummy_plan", exchanges) is None
    assert repeat_escalation_reason([no_threshold], "dummy_hub", "dummy_plan", exchanges) is None


def test_the_count_is_made_by_the_writer_side_helper_and_not_by_the_validator():
    """연속 횟수 판단은 작성agent 쪽 헬퍼이고 검증agent 모듈을 쓰지 않는다."""
    assert "validation_agent" not in vars(round_repeat) and not hasattr(round_repeat, "validate_job")
