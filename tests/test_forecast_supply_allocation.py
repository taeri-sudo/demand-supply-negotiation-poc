import pytest

from sop.access import StateStore
from sop.analysis_stub import get_stub_candidates
from sop.forecast_supply_allocation import (
    allocate_forecast_candidate,
    run_forecast_select_and_allocate,
)
from sop.state import ForecastAgentRecord, RolePermission, State

pytestmark = pytest.mark.anyio


def make_store() -> StateStore:
    """capacity_pools/interaction_protocol 권한을 일부러 안 준다 — 정방향
    최적화 배분은 이제 이 둘 중 어느 것도 참조하지 않는다는 걸 테스트
    자체로 보인다(참조했다면 PermissionDenied로 즉시 드러남)."""
    state = State(
        forecast_agents=[ForecastAgentRecord(agent_id="FCT-1")],
        role_permissions=[
            RolePermission(role_tag="forecast", field_path="forecast_agents", access="r"),
            RolePermission(role_tag="forecast", field_path="forecast_agents", access="w"),
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
    """우선순위 경쟁이 없는 M1에서는 candidate 값이 그대로 allocation이 된다."""
    candidate = allocate_forecast_candidate("FCT-1", 100.0)

    assert candidate.allocation == {"FCT-1": 100.0}
    assert candidate.status == "generated"
    assert candidate.exchanges == []
    assert candidate.required_stages == []


async def test_run_forecast_select_and_allocate_creates_one_allocation_candidate():
    """candidate 선택(confidence 최고인 'a', value=100) → allocation_candidate
    1개 생성, 라운드/capacity 비교 없이 값이 그대로 전달된다."""
    store = make_store()

    candidate = await run_forecast_select_and_allocate(
        store, "forecast", "supply_coordination", 0, get_stub_candidates()
    )

    assert candidate.allocation == {"FCT-1": 100.0}
    assert candidate.status == "generated"

    allocation_candidates = store.get_field("supply_coordination", "allocation_candidates")
    assert allocation_candidates == [candidate]

    agent = store.get_field("forecast", "forecast_agents[0]")
    assert agent.selected == "a"
    assert agent.selection_basis == "rule"


async def test_run_forecast_select_and_allocate_logs_selection_then_generation_in_order():
    store = make_store()

    candidate = await run_forecast_select_and_allocate(
        store, "forecast", "supply_coordination", 0, get_stub_candidates()
    )

    log_events = [entry.event for entry in store.get_field("forecast", "negotiation_log")]
    assert log_events == [
        "candidate_selected_a",
        f"allocation_candidate_generated_{candidate.plan_id}",
    ]
