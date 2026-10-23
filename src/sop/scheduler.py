"""스케줄러: 정해진 시각의 조율과 escalation 시한을 담당 채널로 알린다 (GRAPH_FLOW.md "동시성 모델" 스케줄러).

- 하루 한 번 정해진 시각(`coordination_schedule.DAILY_COORDINATION_TIME`)마다 supply_coordination 채널로 신호를 보낸다. 가리킬 기록이 없어
  신호에는 보낸 시각(`at`)만 담긴다.
- 처리되지 않은 escalation 기록(`deadline`이 있는 것)의 시한이 지나면 그 기록을 만든 agent의 채널로 그 escalation 기록을 가리키는 신호를 보낸다.

신호는 초인종일 뿐이다. 받는 쪽이 State를 읽어 시한이 지났고 아직 처리되지 않았을 때만 시한 초과 처리를 하므로, 같은 신호가 다시 가도 결과가
같다. 한 번 보낸 시한 신호는 기억해 다시 보내지 않고, 받는 쪽이 확인하지 않으면 큐가 그 신호를 다시 나타나게 한다(`signal_queue.py`).
시각은 주입한 `now`로 잰다(테스트에서는 시각을 옮기며 `tick`을 부른다).
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .access import StateStore, signal
from .coordination_schedule import next_daily_coordination
from .escalation_records import OPEN_STATUS
from .logging_utils import log

SCHEDULER_ROLE_TAG = "scheduler"
DAILY_COORDINATION = "daily_coordination"  # `tick`이 돌려주는 보낸 신호의 이름(신호 자체에는 이름이 없다)
DEADLINE = "escalation_deadline"
SUPPLY_COORDINATION_CHANNEL = "supply_coordination"
MAX_IDLE_SECONDS = 60.0  # 다음 신호 시각이 멀어도 이 간격마다 깨어나 새로 생긴 시한을 본다(초기값)

Clock = Callable[[], datetime]


class Scheduler:
    """스케줄러 하나. `tick(now)`가 그 시각까지 도래한 신호를 모두 보내고 보낸 (채널, 종류) 목록을 돌려준다."""

    def __init__(self, store: StateStore, started_at: datetime, role_tag: str = SCHEDULER_ROLE_TAG) -> None:
        self.store = store
        self.role_tag = role_tag
        self._next_daily = next_daily_coordination(started_at)
        self._sent_deadlines: set[int] = set()  # 시한 신호를 이미 보낸 escalation 기록 번호

    def tick(self, now: datetime) -> list[tuple[str, str]]:
        sent: list[tuple[str, str]] = []
        if self._next_daily <= now:  # 여러 날을 건너뛰었어도 신호는 한 번이다(받는 쪽은 상태를 읽어 일한다)
            self._next_daily = next_daily_coordination(max(self._next_daily, now))
            self.store.queue(SUPPLY_COORDINATION_CHANNEL).put_nowait({"at": now.isoformat()})  # 가리킬 기록이 없는 유일한 신호
            sent.append((SUPPLY_COORDINATION_CHANNEL, DAILY_COORDINATION))
        for index, record in enumerate(self.store.get_field(self.role_tag, "escalation_records")):
            if index in self._sent_deadlines or record.mode != "intervention" or record.status != OPEN_STATUS or record.deadline is None:
                continue
            if datetime.fromisoformat(record.deadline) > now:
                continue
            creator = record.trigger_edge.split("->")[0]
            path = f"escalation_records[{index}]"
            self.store.queue(creator).put_nowait({**signal(path, self.role_tag), "at": now.isoformat()})
            self._sent_deadlines.add(index)
            sent.append((creator, DEADLINE))
        if sent:
            log(self.role_tag, "tick", now=now.isoformat(), sent=sent)
        return sent

    def seconds_until_next(self, now: datetime) -> float:
        """다음 정해진 조율 시각이나 아직 신호를 보내지 않은 시한까지 남은 초(`MAX_IDLE_SECONDS`를 넘지 않는다)."""
        due = [self._next_daily]
        for index, record in enumerate(self.store.get_field(self.role_tag, "escalation_records")):
            if index not in self._sent_deadlines and record.mode == "intervention" and record.status == OPEN_STATUS and record.deadline is not None:
                due.append(datetime.fromisoformat(record.deadline))
        return max(0.0, min(MAX_IDLE_SECONDS, (min(due) - now).total_seconds()))

    async def run(
        self, now_fn: Clock = lambda: datetime.now(timezone.utc), sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    ) -> None:
        """시각이 도래할 때마다 신호를 보낸다. 취소될 때까지 돈다."""
        while True:
            now = now_fn()
            self.tick(now)
            await sleep(self.seconds_until_next(now))
