"""forecast의 신호 처리: 채널의 신호를 받아 기록의 상태를 읽고 할 일이 있을 때만 일한다 (GRAPH_FLOW.md "신호 규칙").

신호는 기록의 위치만 담고, 할 일은 신호가 가리키는 기록의 상태로 정한다.
- `forecast_records[i]`: 그 인스턴스의 기록이 "판정됨"이나 "되돌려짐"이면 `check_forecast_verdict`로 다음 agent를 고르거나 재실행한다.
  기록이 "상태 없음"이면 forecast가 만든 escalation 기록이 처리돼 이 신호가 온 것이다: `forecast_run_error`로 첫 실행이 실패해
  `system_operator`가 `rerun`으로 처리했으면 첫 실행을 다시 한다.
- `escalation_records[i]`: 스케줄러가 보낸 시한 신호. 시한이 지난 forecast의 escalation 기록의 시한 초과 처리를 한다.

이번 주기에서 뺀(`dismiss`) 인스턴스와 시한 초과로 예측 수요 없이 끝난 인스턴스는 실행하지도 재실행하지도 않는다. 인스턴스 입력은 호출한 쪽이
(회사, item)마다 `ForecastInstanceRuntime`으로 넘기고, 재실행에 쓰는 직전 결과는 그 객체 안에만 있다(State에 두지 않는다).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .access import StateStore
from .escalation_records import expired_without_demand, is_dismissed
from .external_data import InstanceInputs
from .forecast_escalation_expiry import expire_forecast_escalation
from .forecast_steps import ForecastRerunHandling, run_forecast_steps
from .forecast_supply_allocation import check_forecast_verdict, run_forecast_steps_select_and_submit
from .logging_utils import log

ROLE_TAG = "forecast"


@dataclass
class ForecastInstanceRuntime:
    """forecast 인스턴스 하나((회사, item))의 실행 입력과, 재실행에 쓰는 직전 결과(`handling`). 첫 실행이 끝나면 `handling`이 채워진다."""

    inputs: InstanceInputs
    scheduled_promotion: bool = False
    handling: ForecastRerunHandling | None = None

    def ensure_handling(self) -> ForecastRerunHandling:
        """직전 결과가 없으면(예: 첫 실행 없이 재시작) 같은 입력으로 내부 단계를 다시 돌려 만든다. 같은 입력이면 같은 결과가 나온다."""
        if self.handling is None:
            self.handling = ForecastRerunHandling(self.inputs, run_forecast_steps(self.inputs, self.scheduled_promotion))
        return self.handling


async def run_first_run(store: StateStore, runtime: ForecastInstanceRuntime, role_tag: str = ROLE_TAG) -> bool:
    """첫 실행을 하고 직전 결과를 보관한다. 기록이 "검증 대기"가 되면 True다."""
    result, submitted = await run_forecast_steps_select_and_submit(store, role_tag, runtime.inputs, runtime.scheduled_promotion)
    if result is not None:
        runtime.handling = ForecastRerunHandling(runtime.inputs, result)
    return submitted


async def handle_forecast_signal(
    store: StateStore,
    message: Mapping[str, Any],
    instances: Mapping[tuple[str, str], ForecastInstanceRuntime],
    now: datetime | None = None,
    role_tag: str = ROLE_TAG,
) -> str:
    """신호 하나를 처리하고 결과를 돌려준다: `"first_run"`, `"expired"`, `"dismissed"`, `"no_demand"`, `"skipped"` 또는 `check_forecast_verdict`의 결과."""
    path: str = message["record_path"]
    if path.startswith("escalation_records["):
        index = int(path[path.index("[") + 1 : -1])
        at = now if now is not None else (datetime.fromisoformat(message["at"]) if "at" in message else None)  # 스케줄러가 신호를 보낸 시각
        return "expired" if expire_forecast_escalation(store, role_tag, index, at) != "waiting" else "skipped"
    record_path = path
    record = store.get_field(role_tag, record_path)
    runtime = instances.get((record.company_id, record.item_id))
    if runtime is None:
        raise ValueError(f"인스턴스의 실행 입력이 없음: {record.company_id}/{record.item_id}")
    escalations = store.get_field(role_tag, "escalation_records")
    if is_dismissed(escalations, agent_id=record.agent_id):
        log(role_tag, "handle_forecast_signal", agent_id=record.agent_id, skipped="dismissed")
        return "dismissed"
    state = store.record_state(role_tag, record_path)
    if state == "none":
        if record.scenario is None and expired_without_demand(escalations, record.agent_id):
            return "no_demand"  # 시한이 지나 예측 수요 없이 끝났다
        await run_first_run(store, runtime, role_tag)  # 첫 실행이 실패해 system_operator가 rerun으로 처리했다
        return "first_run"
    return await check_forecast_verdict(store, role_tag, runtime.ensure_handling())
