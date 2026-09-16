import pytest

from sop.access import StateStore
from sop.analysis_stub import get_stub_candidates
from sop.forecast_supply_round import run_forecast_negotiation, run_forecast_supply_round
from sop.state import (
    CapacityPool,
    ForecastAgentRecord,
    InteractionProtocol,
    RolePermission,
    State,
)

pytestmark = pytest.mark.anyio


def make_store(remaining: float) -> StateStore:
    state = State(
        forecast_agents=[ForecastAgentRecord(agent_id="FCT-1")],
        capacity_pools=[
            CapacityPool(
                pool_id="POOL-1",
                total_capacity=100,
                remaining_capacity=remaining,
                linked_role_tags=["forecast"],
            )
        ],
        interaction_protocol=[
            InteractionProtocol(
                edge="forecast<->supply_coordination",
                max_rounds=3,
                repeat_escalation_threshold=2,
                scope=["forecast", "supply_coordination"],
                escalation_trigger="max_rounds_exhausted",
                escalation_target="human_manager",
                escalation_kind="rule",
                source="initial_design",
            )
        ],
        role_permissions=[
            RolePermission(role_tag="forecast", field_path="forecast_agents", access="r"),
            RolePermission(role_tag="forecast", field_path="forecast_agents", access="w"),
            RolePermission(role_tag="forecast", field_path="interaction_protocol", access="r"),
            RolePermission(role_tag="forecast", field_path="negotiation_log", access="r"),
            RolePermission(role_tag="forecast", field_path="negotiation_log", access="w"),
            RolePermission(role_tag="forecast", field_path="escalation_records", access="r"),
            RolePermission(role_tag="forecast", field_path="escalation_records", access="w"),
            RolePermission(role_tag="supply_coordination", field_path="capacity_pools", access="r"),
        ],
    )
    return StateStore(state)


async def test_negotiation_converges_within_max_rounds():
    """remaining=70, 최초 제안 100 → R1: 100→85, R2: 85→77.5(변화율<10%)로 수렴."""
    store = make_store(remaining=70)

    final_value = await run_forecast_supply_round(
        store, "forecast", "supply_coordination", 0, "POOL-1", initial_proposed=100
    )

    assert final_value == pytest.approx(77.5)

    agent = store.get_field("forecast", "forecast_agents[0]")
    assert [r.round for r in agent.round_history] == [1, 2]
    assert agent.round_history[0].proposed["quantity"] == 100
    assert agent.round_history[0].response == {"status": "counter", "quantity": 70}
    assert agent.round_history[1].proposed["quantity"] == 85
    assert agent.round_history[1].response == {"status": "counter", "quantity": 70}
    assert agent.current_round == 2

    assert store.get_field("forecast", "escalation_records") == []
    log_events = [entry.event for entry in store.get_field("forecast", "negotiation_log")]
    assert log_events == ["round_1_counter", "round_2_counter", "negotiation_converged"]


async def test_negotiation_escalates_after_max_rounds_exhausted():
    """remaining=10, 최초 제안 100 → 3라운드 모두 변화율>=10%로 수렴 못 하고 escalation."""
    store = make_store(remaining=10)

    final_value = await run_forecast_supply_round(
        store, "forecast", "supply_coordination", 0, "POOL-1", initial_proposed=100
    )

    assert final_value is None

    agent = store.get_field("forecast", "forecast_agents[0]")
    assert [r.round for r in agent.round_history] == [1, 2, 3]
    assert agent.round_history[0].proposed["quantity"] == 100
    assert agent.round_history[1].proposed["quantity"] == 55
    assert agent.round_history[2].proposed["quantity"] == 32.5

    escalations = store.get_field("forecast", "escalation_records")
    assert len(escalations) == 1
    assert escalations[0].trigger_edge == "forecast<->supply_coordination"
    assert escalations[0].reason == "max_rounds_exhausted"
    assert escalations[0].target_role == "human_manager"
    assert escalations[0].status == "open"

    log_events = [entry.event for entry in store.get_field("forecast", "negotiation_log")]
    assert log_events == [
        "round_1_counter",
        "round_2_counter",
        "round_3_counter",
        "escalation_triggered",
    ]


async def test_negotiation_accepts_immediately_when_capacity_covers_proposal():
    """remaining_capacity가 최초 제안 이상이면 1라운드 만에 accepted로 끝난다."""
    store = make_store(remaining=150)

    final_value = await run_forecast_supply_round(
        store, "forecast", "supply_coordination", 0, "POOL-1", initial_proposed=100
    )

    assert final_value == 100
    agent = store.get_field("forecast", "forecast_agents[0]")
    assert [r.round for r in agent.round_history] == [1]
    assert agent.round_history[0].response == {"status": "accepted", "quantity": 100}
    assert store.get_field("forecast", "escalation_records") == []


async def test_run_forecast_negotiation_selects_candidate_then_negotiates():
    """analysis 스텁 candidate 선택(confidence 최고인 'a', value=100) →
    그 값을 최초 제안으로 협상 → remaining=70이면 77.5로 수렴."""
    store = make_store(remaining=70)

    final_value = await run_forecast_negotiation(
        store, "forecast", "supply_coordination", 0, "POOL-1", get_stub_candidates()
    )

    assert final_value == pytest.approx(77.5)

    agent = store.get_field("forecast", "forecast_agents[0]")
    assert agent.selected == "a"
    assert agent.selection_basis == "rule"
    assert agent.round_history[0].proposed["quantity"] == 100

    log_events = [entry.event for entry in store.get_field("forecast", "negotiation_log")]
    assert log_events[0] == "candidate_selected_a"
