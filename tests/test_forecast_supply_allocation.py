import pytest

from sop.access import StateStore
from sop.analysis_stub import get_stub_scenarios
from sop.forecast_supply_allocation import (
    allocate_forecast_candidate,
    run_forecast_select_and_allocate,
)
from sop.state import ForecastRecord, RolePermission, State

pytestmark = pytest.mark.anyio


def make_store() -> StateStore:
    """capacity_pools/interaction_protocol 권한을 일부러 안 준다 — 정방향
    최적화 배분은 이제 이 둘 중 어느 것도 참조하지 않는다는 걸 테스트
    자체로 보인다(참조했다면 PermissionDenied로 즉시 드러남).

    forecast_records는 같은 회사가 서로 다른 item을 동시에 주문하는 경우를
    표현하려고 (company_id, item_id) 인스턴스 2개를 둔다(라면/과자 —
    STATE_SCHEMA.md "forecast_records[]" 예시와 동일)."""
    state = State(
        forecast_records=[
            ForecastRecord(agent_id="COMPANY-A:RAMEN", company_id="COMPANY-A", item_id="RAMEN"),
            ForecastRecord(agent_id="COMPANY-A:SNACK", company_id="COMPANY-A", item_id="SNACK"),
        ],
        role_permissions=[
            RolePermission(role_tag="forecast", field_path="forecast_records", access="r"),
            RolePermission(role_tag="forecast", field_path="forecast_records", access="w"),
            RolePermission(role_tag="forecast", field_path="negotiation_log", access="r"),
            RolePermission(role_tag="forecast", field_path="negotiation_log", access="w"),
            RolePermission(
                role_tag="supply_coordination", field_path="allocation_candidates", access="r"
            ),
            RolePermission(
                role_tag="supply_coordination", field_path="allocation_candidates", access="w"
            ),
            RolePermission(
                role_tag="supply_coordination", field_path="negotiation_log", access="r"
            ),
            RolePermission(
                role_tag="supply_coordination", field_path="negotiation_log", access="w"
            ),
        ],
    )
    return StateStore(state)


def test_allocate_forecast_candidate_carries_value_through_unchanged():
    """우선순위 경쟁이 없는 M1에서는 선택된 시나리오 값이 그대로 allocation이 된다."""
    candidate = allocate_forecast_candidate("FCT-1", 100.0)

    assert candidate.allocation == {"FCT-1": 100.0}
    assert candidate.status == "generated"
    assert candidate.exchanges == []
    assert candidate.required_stages == []


async def test_run_forecast_select_and_allocate_creates_one_allocation_candidate():
    """시나리오 선택(cost_estimate 최저인 'b', value=120) → allocation_candidate
    1개 생성, 라운드/capacity 비교 없이 값이 그대로 전달된다."""
    store = make_store()

    candidate = await run_forecast_select_and_allocate(
        store,
        "forecast",
        "supply_coordination",
        "COMPANY-A",
        "RAMEN",
        get_stub_scenarios("COMPANY-A", "RAMEN"),
    )

    assert candidate.allocation == {"COMPANY-A:RAMEN": 120.0}
    assert candidate.status == "generated"

    allocation_candidates = store.get_field("supply_coordination", "allocation_candidates")
    assert allocation_candidates == [candidate]

    record = store.get_field("forecast", "forecast_records[0]")
    assert record.selected_scenario == "b"
    assert record.selection_basis == "rule"


async def test_run_forecast_select_and_allocate_logs_selection_then_generation_in_order():
    store = make_store()

    candidate = await run_forecast_select_and_allocate(
        store,
        "forecast",
        "supply_coordination",
        "COMPANY-A",
        "RAMEN",
        get_stub_scenarios("COMPANY-A", "RAMEN"),
    )

    log_events = [entry.event for entry in store.get_field("forecast", "negotiation_log")]
    assert log_events == [
        "scenario_selected_b",
        f"allocation_candidate_generated_{candidate.plan_id}",
    ]


async def test_run_forecast_select_and_allocate_targets_only_matching_instance():
    """(company_id, item_id)로 조회하므로, 먼저 처리되는 인스턴스가 뒤 인덱스여도
    올바른 인스턴스만 갱신되고 나머지 인스턴스(과자를 처리했으니 라면)는
    영향받지 않는다 — 리스트 인덱스 하나로만 고르던 이전 방식이면 드러나지
    않는 문제(엉뚱한 인스턴스를 갱신)를 잡아낸다."""
    store = make_store()

    candidate = await run_forecast_select_and_allocate(
        store,
        "forecast",
        "supply_coordination",
        "COMPANY-A",
        "SNACK",
        get_stub_scenarios("COMPANY-A", "SNACK"),
    )

    # SNACK 시나리오 중 cost_estimate 최저는 'b'(280, value=80) — RAMEN(value=120)과 다른 값
    assert candidate.allocation == {"COMPANY-A:SNACK": 80.0}

    snack_record = store.get_field("forecast", "forecast_records[1]")
    assert snack_record.selected_scenario == "b"
    assert snack_record.selection_basis == "rule"

    ramen_record = store.get_field("forecast", "forecast_records[0]")
    assert ramen_record.selected_scenario is None
    assert ramen_record.selection_basis is None
