"""capacity_pools 증감을 위한 Lock 보호 헬퍼.

asyncio는 단일 프로세스·단일 스레드지만, get_field로 읽은 뒤 set_field로
쓰기까지 사이에 await 지점이 끼면 다른 태스크가 끼어들어 갱신이 유실될 수
있다(race condition). capacity_pools처럼 여러 태스크가 동시에 건드리는
공유자원은 asyncio.Lock으로 read-modify-write 구간을 감싼다
(STATE_SCHEMA.md "공유 자원 보호").
"""

from .access import StateStore
from .ids import now_iso
from .state import CapacityAdjustment


async def adjust_capacity_pool(
    store: StateStore,
    role_tag: str,
    pool_id: str,
    delta: float,
    reason: str,
    *,
    notify_channel: str,
) -> None:
    async with store.lock(f"capacity_pools:{pool_id}"):
        pools = store.get_field(role_tag, "capacity_pools")
        index = next(i for i, pool in enumerate(pools) if pool.pool_id == pool_id)
        pool = pools[index]
        updated = pool.model_copy(
            update={
                "remaining_capacity": pool.remaining_capacity + delta,
                "adjustment_history": [
                    *pool.adjustment_history,
                    CapacityAdjustment(ts=now_iso(), delta=delta, reason=reason),
                ],
            }
        )
        store.set_field(
            role_tag, f"capacity_pools[{index}]", updated, notify_channel=notify_channel
        )
