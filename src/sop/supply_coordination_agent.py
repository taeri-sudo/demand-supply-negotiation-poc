"""supply_coordination의 신호 처리: 채널의 신호를 받아 라인의 상태를 읽고 할 일이 있을 때만 조율한다 (GRAPH_FLOW.md "조율 흐름", "신호 규칙").

신호는 기록의 위치(`record_path`)만 담은 초인종이고 받는 쪽이 State를 직접 읽어 판단하므로 같은 신호가 여러 번 와도 결과가 같다. 신호가 가리키는 기록:
- `forecast_records[i]`: 그 기록이 "전달됨"이 되었거나(받는다. supply_coordination 카드의 `accepts`와 맞지 않으면 `misrouted`로 되돌린다), 보류가 시작되거나 끝났다.
  어느 쪽이든 그 기록의 라인을 확인한다.
- `allocations[i]`: 라인의 결론이 판정됐거나, supply_coordination이 만든 그 라인의 escalation 기록이 처리됐거나, 외부 데이터가 바뀌었다는 sales_channel의 알림이다
  (`notify_line_changed`). 그 라인을 확인한다.
- 기록을 가리키지 않는 신호(`at`만 있음)는 스케줄러가 정해진 조율 시각에 보낸다. 모든 라인의 차이로 남은 변경을 조율한다.

라인 확인(`check_line`)은 라인의 `allocations` 기록과 forecast 인스턴스, 입력의 변경을 읽는다.
1. 보류(업무 대기·시스템 대기) 중이거나 이번 주기에서 뺀 라인이면 아무것도 하지 않는다.
2. 결론이 `failed`로 판정됐으면 `check_allocation_verdict`의 처리를 따른다(입력이 달라졌거나 `system_operator`가 `rerun`했으면 다시 조율).
3. 이번 주기의 정기 조율을 마치지 않았으면(`allocations`의 `cycle`) 시작 규칙이 허락할 때만 정기 조율한다. 아니면 변경 신호에도 아무것도 하지 않는다
   (정기 조율이 변경을 모두 함께 넣는다).
4. 정기 조율을 마쳤으면 마지막 결론의 입력과 지금 입력의 차이(변경)를 본다. 사람의 처리 결과·사람이 정한 수량·급한 변경이면 바로, 그 밖의 자동
   변경은 정해진 조율 시각의 신호에 이어지는 조율한다. 조율을 마치면 그새 모인 변경이 있는지 다시 본다. 연속 조율이 `MAX_ROUNDS`에 닿으면 남은
   변경을 놓치지 않도록 supply_coordination 채널에 그 라인의 신호를 다시 넣는다.
"""

from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .access import StateStore, signal
from .coordination_changes import diff_inputs, immediate
from .coordination_flow import (
    ComputeDelay,
    NoProcessingInstances,
    RegularCoordinationStartRule,
    coordinate_line,
    decided_inputs,
    instance_statuses,
)
from .coordination_inputs import CoordinationSettings, LineDataProvider, LineOf, build_line_inputs, human_forecast_refs
from .escalation_records import is_dismissed
from .forecast_supply_allocation import receive_forwarded_forecast
from .logging_utils import log
from .record_state import HOLD_STATES
from .supply_coordination_line import check_allocation_verdict, find_allocation_index

ROLE_TAG = "supply_coordination"
MAX_ROUNDS = 10  # 라인 확인 하나에서 이어서 조율하는 최대 횟수(안전장치. 조율마다 마지막 결론의 입력이 갱신돼 보통 한두 번에 끝난다)

LineCheck = Literal[
    "held", "dismissed", "invalid", "waiting", "deferred", "idle", "coordinated", "escalated", "no_forecast_record"
]


@dataclass(frozen=True)
class LineCheckResult:
    outcome: LineCheck
    coordinations: int = 0  # 이 확인에서 조율한(결론을 쓴 것과 보류한 것을 합친) 횟수


class SupplyCoordinator:
    """supply_coordination 에이전트 하나. 라인 목록은 `capacity_pools`의 라인이다.

    `compute_delay`는 계산이 오래 걸리는 구간을 대신하는 await 지점이다(M7의 LLM 응답 대기 자리). 기다리는 동안 들어오는 값은 조율에 끼어들지
    않고 결론 직전의 확인에서 마지막 순서로 얹힌다.
    """

    def __init__(
        self,
        store: StateStore,
        settings: CoordinationSettings,
        provider: LineDataProvider,
        line_of: LineOf,
        start_rule: RegularCoordinationStartRule | None = None,
        compute_delay: ComputeDelay | None = None,
        role_tag: str = ROLE_TAG,
    ) -> None:
        self.store = store
        self.settings = settings
        self.provider = provider
        self.line_of = line_of
        self.start_rule: RegularCoordinationStartRule = start_rule if start_rule is not None else NoProcessingInstances()
        self.compute_delay = compute_delay
        self.role_tag = role_tag

    @property
    def lines(self) -> list[str]:
        return [pool.line_id for pool in self.store.get_field(self.role_tag, "capacity_pools")]

    # --- 신호 -----------------------------------------------------------------------------------------------------

    async def handle_signal(self, message: Mapping[str, Any]) -> list[LineCheckResult]:
        """신호 하나를 처리한다. 확인한 라인마다 결과를 돌려준다(일이 없으면 빈 목록 또는 `idle` 등)."""
        path = message.get("record_path")
        if path is None:  # 스케줄러가 정해진 조율 시각에 보낸 신호
            return [await self.check_line(line_id, include_deferred=True) for line_id in self.lines]
        if path.startswith("allocations["):
            return [await self.check_line(self.store.get_field(self.role_tag, path).line_id)]
        if path.startswith("forecast_records["):
            receive_forwarded_forecast(self.store, self.role_tag, message)  # "전달됨"이면 받고, supply_coordination의 일이 아니면 `misrouted`로 되돌린다
            record = self.store.get_field(self.role_tag, path)
            return [await self.check_line(self.line_of(record.item_id))]
        return []

    async def run(self) -> None:
        """채널의 신호를 하나씩 받아 처리한다. 처리를 끝낸 신호만 확인(`ack`)하고, 처리 중 예외가 나면 확인하지 않아 신호가 다시 나타난다."""
        queue = self.store.queue(self.role_tag)
        while True:
            message = await queue.get()
            try:
                await self.handle_signal(message)
            except Exception as error:  # 이 신호만 실패시키고 채널은 계속 받는다
                log(self.role_tag, "run", skipped_ack=True, error=f"{type(error).__name__}: {error}")
                continue
            queue.ack(message)

    # --- 라인 확인 ---------------------------------------------------------------------------------------------------

    async def check_line(self, line_id: str, include_deferred: bool = False) -> LineCheckResult:
        """라인의 상태를 읽어 할 일이 있을 때만 조율한다. 조율을 마치면 그새 모인 변경이 있는지 다시 보고, 있으면 이어서 조율한다."""
        coordinations = 0
        for _ in range(MAX_ROUNDS):
            outcome = await self._check_once(line_id, include_deferred)
            if outcome == "coordinated":
                coordinations += 1
                continue
            # 조율한 뒤 더 할 일이 없으면 조율한 것으로 보고한다
            return LineCheckResult("coordinated" if coordinations and outcome in ("idle", "deferred") else outcome, coordinations)
        # 연속 조율이 상한에 닿았다. 이 신호를 확인하면 남은 변경이 다음 신호까지 처리되지 않으므로 그 라인의 신호를 다시 넣는다
        index = find_allocation_index(self.store, self.role_tag, line_id)
        self.store.queue(self.role_tag).put_nowait(signal(f"allocations[{index}]", self.role_tag))
        log(self.role_tag, "check_line", line_id=line_id, rounds=MAX_ROUNDS, requeued=True)
        return LineCheckResult("coordinated", coordinations)

    async def _check_once(self, line_id: str, include_deferred: bool) -> LineCheck:
        store, role = self.store, self.role_tag
        index = find_allocation_index(store, role, line_id)
        path = f"allocations[{index}]"
        allocation = store.get_field(role, path)
        if store.record_state(role, path) in HOLD_STATES:
            return "held"
        if is_dismissed(store.get_field(role, "escalation_records"), line_id=line_id):
            return "dismissed"
        current = build_line_inputs(store, role, self.settings, self.provider, self.line_of, line_id)
        human = human_forecast_refs(store, role)

        if store.record_state(role, path) == "judged" and allocation.validation is not None and allocation.validation.status == "failed":
            action = check_allocation_verdict(store, role, line_id, current)
            if action == "invalid":
                return "invalid"
            if action == "dismissed":
                return "dismissed"
            if action == "recoordinate":
                return await self._coordinate(line_id, continuing=True)

        if allocation.cycle != self.settings.cycle:  # 이번 주기의 정기 조율을 마치지 않았다
            statuses = instance_statuses(store, role, line_id, self.line_of)
            if not self.start_rule.ready(line_id, statuses):
                return "waiting"
            return await self._coordinate(line_id, continuing=False)

        previous = decided_inputs(store, role, allocation)
        changes = diff_inputs(previous, current, human, self.settings.freeze_days) if previous is not None else []
        if not changes:
            return "idle"
        if not (immediate(changes) or include_deferred):
            return "deferred"
        return await self._coordinate(line_id, continuing=True)

    async def _coordinate(self, line_id: str, continuing: bool) -> LineCheck:
        result = await coordinate_line(
            self.store, self.role_tag, line_id, settings=self.settings, provider=self.provider, line_of=self.line_of,
            continuing=continuing, compute_delay=self.compute_delay,
        )
        return "coordinated" if result.outcome == "allocated" else "escalated"



def notify_line_changed(store: StateStore, line_id: str, written_by: str = "sales_channel", role_tag: str = ROLE_TAG) -> None:
    """외부 데이터(수주, 사람이 정한 수량 등)가 바뀌었다는 sales_channel의 알림. 그 라인의 `allocations` 기록을 가리키는 신호를 보낸다."""
    index = find_allocation_index(store, role_tag, line_id)
    store.queue(role_tag).put_nowait(signal(f"allocations[{index}]", written_by))
