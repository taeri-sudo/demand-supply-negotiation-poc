import asyncio

import pytest

from sop.access import StateStore
from sop.capacity import adjust_capacity_pool
from sop.state import CapacityPool, RolePermission, State

pytestmark = pytest.mark.anyio


def make_store(remaining: float = 100) -> StateStore:
    state = State(
        capacity_pools=[
            CapacityPool(pool_id="POOL-1", total_capacity=100, remaining_capacity=remaining)
        ],
        role_permissions=[
            RolePermission(role_tag="supply_coordination", field_path="capacity_pools", access="r"),
            RolePermission(role_tag="supply_coordination", field_path="capacity_pools", access="w"),
        ],
    )
    return StateStore(state)


async def _unsafe_decrement(store: StateStore, amount: float) -> None:
    """Lock 없이 get_field → (yield) → set_field로 직접 감소.

    read와 write 사이에 await 지점을 둬, 다른 태스크가 끼어들 여지를 만든다
    (LLM 응답 대기 등 실제 agent 코드에서 자연히 생기는 상황을 흉내).
    """
    pools = store.get_field("supply_coordination", "capacity_pools")
    pool = pools[0]
    await asyncio.sleep(0)  # 다른 태스크가 끼어들 기회
    updated = pool.model_copy(update={"remaining_capacity": pool.remaining_capacity - amount})
    store.set_field(
        "supply_coordination", "capacity_pools[0]", updated, notify_channel="capacity_pools"
    )


async def _locked_decrement(store: StateStore, amount: float) -> None:
    """_unsafe_decrement와 동일한 로직이되, Lock으로 read-modify-write를 감싼다."""
    async with store.lock("capacity_pools:POOL-1"):
        pools = store.get_field("supply_coordination", "capacity_pools")
        pool = pools[0]
        await asyncio.sleep(0)
        updated = pool.model_copy(update={"remaining_capacity": pool.remaining_capacity - amount})
        store.set_field(
            "supply_coordination", "capacity_pools[0]", updated, notify_channel="capacity_pools"
        )


async def test_concurrent_decrement_without_lock_loses_updates():
    """Lock 없이 동시에 감소시키면 한쪽 갱신이 유실되어 최종값이 틀려진다."""
    store = make_store(remaining=100)

    await asyncio.gather(
        _unsafe_decrement(store, 30),
        _unsafe_decrement(store, 30),
    )

    remaining = store.get_field("supply_coordination", "capacity_pools")[0].remaining_capacity
    # 두 태스크 모두 100을 읽은 뒤 각자 70으로 써서, 두 번째 쓰기가 첫 번째를 덮어씀
    assert remaining == 70
    assert remaining != 40  # 정확한 값(100 - 30 - 30)과 다름을 직접 확인


async def test_concurrent_decrement_with_lock_is_correct():
    """같은 시나리오를 Lock으로 감싸면 두 감소가 모두 반영된다."""
    store = make_store(remaining=100)

    await asyncio.gather(
        _locked_decrement(store, 30),
        _locked_decrement(store, 30),
    )

    remaining = store.get_field("supply_coordination", "capacity_pools")[0].remaining_capacity
    assert remaining == 40


async def test_adjust_capacity_pool_helper_is_safe_under_concurrency():
    """실제로 다른 마일스톤이 사용할 프로덕션 헬퍼(adjust_capacity_pool)도
    다수 동시 호출 아래에서 배분 합과 remaining_capacity가 일치해야 한다."""
    store = make_store(remaining=100)

    await asyncio.gather(
        *(
            adjust_capacity_pool(
                store,
                "supply_coordination",
                "POOL-1",
                delta=-5,
                reason="test",
                notify_channel="capacity_pools",
            )
            for _ in range(10)
        )
    )

    pool = store.get_field("supply_coordination", "capacity_pools")[0]
    assert pool.remaining_capacity == 50
    assert len(pool.adjustment_history) == 10
