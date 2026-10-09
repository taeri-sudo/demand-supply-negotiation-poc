import pytest

from assumption_fixtures import fixed_assumptions
from sop.access import StateStore
from sop.forecast_supply_allocation import (
    allocate_forecast_candidate,
    run_forecast_select_and_submit,
)
from sop.state import ForecastRecord
from validation_fixtures import make_store as make_pipeline_store, validate_and_allocate

pytestmark = pytest.mark.anyio


def make_store() -> StateStore:
    """forecast_records는 같은 회사가 서로 다른 item을 동시에 주문하는 경우를
    표현하려고 (company_id, item_id) 인스턴스 2개를 둔다(라면/과자 —
    STATE_SCHEMA.md "forecast_records[]" 예시와 동일).

    권한은 forecast·검증agent·supply_coordination 세 역할이 쓰는 필드만 준다 — capacity_pools는 정방향 최적화 배분이
    참조하지 않으므로 권한이 없고, 참조했다면 PermissionDenied로 즉시 드러난다."""
    return make_pipeline_store(
        [
            ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN"),
            ForecastRecord(agent_id="COMPANY-A:SNACK", company_id="COMPANY-A", item_id="SNACK"),
        ]
    )


async def submit(store, item_id):
    return await run_forecast_select_and_submit(
        store, "forecast", "COMPANY-A", item_id, fixed_assumptions(item_id)
    )


def test_allocate_forecast_candidate_carries_value_through_unchanged():
    """우선순위 경쟁이 없는 M1에서는 최종 요청량(시나리오) 값이 그대로 allocation이 된다."""
    candidate = allocate_forecast_candidate("FCT-1", 100.0)

    assert candidate.allocation == {"FCT-1": 100.0}
    assert candidate.status == "generated"
    assert candidate.exchanges == []
    assert candidate.required_stages == []


async def test_run_forecast_select_and_submit_creates_one_allocation_candidate_after_validation():
    """가정 선택(상대 오차가 가장 작아 가장 그럴듯한 'b', value=120) → 검증agent 통과 → allocation_candidate
    1개 생성, 라운드/capacity 비교 없이 값이 그대로 전달된다."""
    store = make_store()

    assert await submit(store, "RAMEN") is True
    assert store.get_field("supply_coordination", "allocation_candidates") == []  # 검증 전에는 만들지 않는다
    (candidate,) = validate_and_allocate(store)

    assert candidate.allocation == {"COMPANY-A:RAMEN": 120.0}
    assert candidate.status == "generated"

    allocation_candidates = store.get_field("supply_coordination", "allocation_candidates")
    assert allocation_candidates == [candidate]

    record = store.get_field("forecast", "forecast_records[0]")
    assert record.scenario is not None
    assert record.scenario.value == 120.0 and record.scenario.assumption_ids == ["b"]
    assert record.scenario.derivation == "chosen"
    assert [a.assumption_id for a in record.assumptions] == ["a", "b", "c"]
    assert record.selection_basis == "rule"
    assert record.validation is not None and record.validation.status == "passed"


async def test_run_forecast_select_and_submit_logs_selection_then_validation_then_generation_in_order():
    store = make_store()

    await submit(store, "RAMEN")
    (candidate,) = validate_and_allocate(store)

    entries = store.negotiation_log("forecast")
    assert [e.event for e in entries] == [
        "scenario_decided", "validation_passed", "forwarded", "allocation_candidate_generated",
    ]  # forecast가 직접 보낸 뒤 supply_coordination이 만든다
    assert [e.seq for e in entries] == sorted(e.seq for e in entries)
    assert all(e.agent_id == "COMPANY-A:RAMEN" for e in entries)
    assert entries[3].payload["plan_id"] == candidate.plan_id


async def test_run_forecast_select_and_submit_targets_only_matching_instance():
    """(company_id, item_id)로 조회하므로, 먼저 처리되는 인스턴스가 뒤 인덱스여도
    올바른 인스턴스만 갱신되고 나머지 인스턴스(과자를 처리했으니 라면)는
    영향받지 않는다 — 리스트 인덱스만으로 고르면 드러나지 않는
    문제(엉뚱한 인스턴스를 갱신)를 잡아낸다."""
    store = make_store()

    await submit(store, "SNACK")
    (candidate,) = validate_and_allocate(store)

    # SNACK 가정 중 가장 그럴듯한 가정은 'b'(value=80) — RAMEN(value=120)과 다른 값
    assert candidate.allocation == {"COMPANY-A:SNACK": 80.0}

    snack_record = store.get_field("forecast", "forecast_records[1]")
    assert snack_record.scenario is not None and snack_record.scenario.assumption_ids == ["b"]
    assert snack_record.selection_basis == "rule"

    ramen_record = store.get_field("forecast", "forecast_records[0]")
    assert ramen_record.scenario is None
    assert ramen_record.selection_basis is None
    assert ramen_record.validation is None


async def test_the_forecast_never_writes_the_allocation_directly():
    """작성agent의 신호는 검증agent 큐로만 가고, supply_coordination 쪽 채널에는 검증 전에 아무것도 가지 않는다."""
    store = make_store()

    await submit(store, "RAMEN")

    assert store.queue("forecast_validation").qsize() == 1
    assert store.queue("supply_coordination").empty() and store.queue("allocation_candidates").empty()
