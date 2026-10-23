import pytest

from sop.access import PermissionDenied, StateStore
from sop.state import CapacityPool, RolePermission, State


def make_store() -> StateStore:
    state = State(
        capacity_pools=[
            CapacityPool(line_id="LINE-1", total_capacity=100, remaining_capacity=100)
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
        )


def test_get_field_denied_without_permission():
    store = make_store()

    with pytest.raises(PermissionDenied):
        store.get_field("procurement_plan", "capacity_pools")


def test_a_write_that_changes_no_records_state_sends_no_signal():
    """상태를 가진 기록이 아닌 필드(`capacity_pools`)를 쓰면 어느 채널에도 신호가 가지 않는다(GRAPH_FLOW.md "신호 규칙")."""
    store = make_store()

    store.set_field("supply_coordination", "capacity_pools", [])

    assert store.state.capacity_pools == []
    assert all(queue.empty() for queue in store._queues.values())  # pyright: ignore[reportPrivateUsage]


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
    assert store.negotiation_log("human_manager") == []


def wildcard_store() -> StateStore:
    pools = [CapacityPool(line_id=f"LINE-{i}", total_capacity=100, remaining_capacity=100) for i in range(3)]
    permissions = [
        RolePermission(role_tag="checker", field_path="capacity_pools[*].remaining_capacity", access="w"),
        RolePermission(role_tag="checker", field_path="capacity_pools", access="r"),
        RolePermission(role_tag="one_pool", field_path="capacity_pools[1]", access="w"),
    ]
    return StateStore(State(capacity_pools=pools, role_permissions=permissions))


def test_instance_index_wildcard_covers_every_instance_but_only_that_field():
    store = wildcard_store()

    for index in range(3):
        store.set_field("checker", f"capacity_pools[{index}].remaining_capacity", 1.0)
    assert [pool.remaining_capacity for pool in store.state.capacity_pools] == [1.0, 1.0, 1.0]
    for denied in ("capacity_pools[0].total_capacity", "capacity_pools[0]", "capacity_pools"):
        with pytest.raises(PermissionDenied):
            store.set_field("checker", denied, None)


def test_a_permission_with_a_number_covers_only_that_instance_and_everything_below_it():
    store = wildcard_store()

    store.set_field("one_pool", "capacity_pools[1].remaining_capacity", 5.0)
    with pytest.raises(PermissionDenied):
        store.set_field("one_pool", "capacity_pools[0].remaining_capacity", 5.0)
    with pytest.raises(PermissionDenied):
        store.set_field("one_pool", "capacity_pools", [])  # 접근 경로가 권한 경로보다 짧다


# --- 보류 쓰기: 로그 항목, escalation 기록, 기록은 한 번의 State 갱신 ----------------------------------------------


def held_store(*, escalation_write=True, log_write=True, record_write=True):
    from sop.state import ForecastRecord

    permissions = []
    if record_write:
        permissions.append(RolePermission(role_tag="f", field_path="forecast_records", access="w"))
    if escalation_write:
        permissions.append(RolePermission(role_tag="f", field_path="escalation_records", access="w"))
    if log_write:
        permissions.append(RolePermission(role_tag="f", field_path="role_logs.f", access="a"))
    records = [ForecastRecord(agent_id="C:I", company_id="C", item_id="I")]
    return StateStore(State(forecast_records=records, role_permissions=permissions))


def held_escalation(store, agent_id="C:I", log_seq=None):
    from sop.state import EscalationRecord

    return EscalationRecord(
        agent_id=agent_id, trigger_edge="forecast->human_manager", reason="no_computable_assumption", rationale="x",
        target_role="human_manager", mode="intervention", log_seq=store.next_log_seq() if log_seq is None else log_seq, status="open",
    )


def hold(store, **overrides):
    record = store.state.forecast_records[0].model_copy(update={"selection_basis": "rule"})
    arguments = dict(
        agent_id="C:I", escalation=held_escalation(store), log_event="scenario_not_computable",
        log_payload={"rationale": "x"}, field_path="forecast_records[0]", value=record,
    )
    arguments.update(overrides)
    return store.write_held("f", **arguments)


def test_a_held_write_applies_the_log_entry_the_escalation_and_the_record_together():
    store = held_store()
    expected_seq = store.next_log_seq()

    entry = hold(store)

    assert store.state.forecast_records[0].selection_basis == "rule"
    (escalation,) = store.state.escalation_records
    assert escalation.log_seq == expected_seq == entry.seq  # seq를 먼저 정해 같은 갱신에서 그 seq로 쓴다
    assert store.state.role_logs["f"] == [entry] and entry.agent_id == "C:I" and entry.payload == {"rationale": "x"}
    sizes = {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]
    assert sizes == {"human_manager": 1}  # 기록이 사람 대기가 되어 human_manager에게만 신호가 간다


def test_a_held_write_without_a_record_writes_only_the_log_entry_and_the_escalation():
    store = held_store()

    hold(store, field_path=None, value=None)

    assert store.state.forecast_records[0].selection_basis is None
    assert len(store.state.escalation_records) == 1 and len(store.state.role_logs["f"]) == 1


def test_an_exception_while_writing_leaves_none_of_the_three():
    store = held_store()
    before = store.state

    with pytest.raises(IndexError):  # 없는 인스턴스 인덱스에 쓰다 예외가 난다
        hold(store, field_path="forecast_records[5]")

    assert store.state is before  # 로그 항목도 escalation 기록도 기록도 반영되지 않았다
    assert store.queue("human_manager").empty()


@pytest.mark.parametrize("missing", ["escalation_write", "log_write", "record_write"])
def test_a_missing_permission_for_any_part_applies_none_of_the_three(missing):
    store = held_store(**{missing: False})
    before = store.state

    with pytest.raises(PermissionDenied):
        hold(store)

    assert store.state is before and store.queue("human_manager").empty()


def test_a_log_seq_that_is_not_the_next_seq_is_rejected():
    store = held_store()
    before = store.state

    with pytest.raises(ValueError, match="seq"):
        hold(store, escalation=held_escalation(store, log_seq=99))

    assert store.state is before


def test_an_instance_that_is_already_held_cannot_be_held_again():
    store = held_store()
    hold(store)
    after_first = store.state

    with pytest.raises(ValueError, match="이미 보류"):
        hold(store)

    assert store.state is after_first  # 아무것도 쓰지 않았다


def test_the_escalation_must_belong_to_the_instance():
    store = held_store()

    with pytest.raises(ValueError):
        hold(store, escalation=held_escalation(store, agent_id="OTHER"))


# --- `a`(이어서 쓰기): 목록 끝에 더하는 쓰기만 허용하고 기존 항목을 고치거나 지우는 쓰기는 거부한다 -------------------------


def append_store(*, also_write: bool = False) -> StateStore:
    from sop.state import EscalationRecord

    permissions = [
        RolePermission(role_tag="f", field_path="escalation_records", access="a"),
        RolePermission(role_tag="f", field_path="escalation_records", access="r"),
        RolePermission(role_tag="f", field_path="role_logs.f", access="a"),
        RolePermission(role_tag="f", field_path="role_logs", access="r"),
    ]
    if also_write:
        permissions.append(RolePermission(role_tag="f", field_path="escalation_records", access="w"))
    first = EscalationRecord(trigger_edge="e", reason="r", rationale="첫째", target_role="human_manager", status="open")
    return StateStore(State(escalation_records=[first], role_permissions=permissions))


def extra_escalation(rationale: str = "둘째"):
    from sop.state import EscalationRecord

    return EscalationRecord(trigger_edge="e", reason="r", rationale=rationale, target_role="system_operator", status="open")


def test_append_permission_allows_adding_to_the_end_of_the_list():
    store = append_store()
    original = store.state.escalation_records[0]

    store.set_field("f", "escalation_records", [original, extra_escalation()])

    assert [r.rationale for r in store.state.escalation_records] == ["첫째", "둘째"]


def test_append_permission_rejects_changing_or_removing_an_existing_item():
    store = append_store()
    original = store.state.escalation_records[0]
    before = store.state

    with pytest.raises(PermissionDenied, match="이어서 쓰기"):
        store.set_field("f", "escalation_records", [original.model_copy(update={"status": "resolved"}), extra_escalation()])  # 고침
    with pytest.raises(PermissionDenied, match="이어서 쓰기"):
        store.set_field("f", "escalation_records", [extra_escalation()])  # 기존 항목을 바꿔치기
    with pytest.raises(PermissionDenied, match="이어서 쓰기"):
        store.set_field("f", "escalation_records", [])  # 지움

    assert store.state is before


def test_append_permission_does_not_reach_below_the_list():
    store = append_store()
    before = store.state

    with pytest.raises(PermissionDenied):
        store.set_field("f", "escalation_records[0].status", "resolved")  # 항목 안의 필드
    with pytest.raises(PermissionDenied):
        store.set_field("f", "escalation_records[0]", before.escalation_records[0])  # 항목 하나

    assert store.state is before


def test_a_write_permission_on_the_same_path_allows_any_change():
    store = append_store(also_write=True)

    store.set_field("f", "escalation_records", [])

    assert store.state.escalation_records == []


def test_the_log_is_appended_with_the_append_permission_and_an_existing_entry_cannot_be_changed():
    store = append_store()
    first = store.append_log("f", "scenario_decided", agent_id="A")
    store.append_log("f", "forecast_run_skipped", agent_id="A")
    assert [e.seq for e in store.state.role_logs["f"]] == [first.seq, first.seq + 1]
    before = store.state

    edited = [store.state.role_logs["f"][0].model_copy(update={"agent_id": "고침"}), store.state.role_logs["f"][1]]
    with pytest.raises(PermissionDenied, match="이어서 쓰기"):
        store.set_field("f", "role_logs.f", edited)  # 기존 항목을 고치는 쓰기
    with pytest.raises(PermissionDenied, match="이어서 쓰기"):
        store.set_field("f", "role_logs.f", store.state.role_logs["f"][:1])  # 지우는 쓰기

    assert store.state is before


def test_a_log_cannot_be_appended_with_only_a_write_permission_or_for_another_role():
    store = StateStore(State(role_permissions=[RolePermission(role_tag="f", field_path="role_logs.f", access="w")]))

    with pytest.raises(PermissionDenied, match="이어서 쓸 권한"):
        store.append_log("f", "scenario_decided")  # 로그는 `a`로 쓴다
    with pytest.raises(PermissionDenied):
        append_store().append_log("other", "scenario_decided")

    assert store.state.role_logs == {}


def test_a_log_path_can_be_read_through_the_wrapper():
    store = append_store()
    entry = store.append_log("f", "scenario_decided", agent_id="A")

    assert store.get_field("f", "role_logs.f") == [entry]
    assert store.get_field("f", "role_logs.unknown_role") == []  # 아직 로그가 없는 역할은 빈 목록이다
