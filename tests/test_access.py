import pytest

from sop.access import PermissionDenied, StateStore
from sop.state import CapacityPool, RolePermission, State


def make_store() -> StateStore:
    state = State(
        capacity_pools=[
            CapacityPool(pool_id="POOL-1", total_capacity=100, remaining_capacity=100)
        ],
        role_permissions=[
            RolePermission(role_tag="supply_coordination", field_path="capacity_pools", access="r"),
            RolePermission(role_tag="supply_coordination", field_path="capacity_pools", access="w"),
        ],
    )
    return StateStore(state)


def test_set_field_denied_without_permission():
    """role_permissions에 없는 role_tag는 남의 필드에 쓸 수 없다."""
    store = make_store()

    with pytest.raises(PermissionDenied):
        store.set_field(
            "procurement_plan",
            "capacity_pools",
            [],
            notify_channel="capacity_pools",
        )


def test_get_field_denied_without_permission():
    store = make_store()

    with pytest.raises(PermissionDenied):
        store.get_field("procurement_plan", "capacity_pools")


def test_set_field_pushes_exactly_one_queue_message():
    """set_field는 값 기록과 큐 push를 항상 짝짓는다(STATE_SCHEMA.md)."""
    store = make_store()

    store.set_field(
        "supply_coordination",
        "capacity_pools",
        [],
        notify_channel="capacity_pools",
    )

    queue = store.queue("capacity_pools")
    assert queue.qsize() == 1
    message = queue.get_nowait()
    assert message["field_path"] == "capacity_pools"
    assert message["written_by"] == "supply_coordination"
    assert queue.empty()


def test_set_field_nested_path_updates_single_element_without_mutating_original():
    """다른 마일스톤에서 exchanges[i].response 같은 중첩 경로 쓰기가 필요하므로
    get_field/set_field가 인덱스가 섞인 경로도 처리하는지 확인한다."""
    store = make_store()
    original_pools = store.get_field("supply_coordination", "capacity_pools")

    updated_pool = original_pools[0].model_copy(update={"remaining_capacity": 40})
    store.set_field(
        "supply_coordination",
        "capacity_pools[0]",
        updated_pool,
        notify_channel="capacity_pools",
    )

    assert store.get_field("supply_coordination", "capacity_pools[0]").remaining_capacity == 40
    # 원본으로 읽었던 리스트/모델은 그대로 남아있어야 함(불변 갱신)
    assert original_pools[0].remaining_capacity == 100


def test_wildcard_permission_grants_full_access():
    """field_path '*'는 role_permissions.md가 명시한 '예외적 전체 접근'."""
    state = State(
        role_permissions=[RolePermission(role_tag="human_manager", field_path="*", access="r")]
    )
    store = StateStore(state)

    # 예외 없이 임의 필드를 읽을 수 있어야 함
    assert store.get_field("human_manager", "escalation_records") == []
    assert store.get_field("human_manager", "negotiation_log") == []
