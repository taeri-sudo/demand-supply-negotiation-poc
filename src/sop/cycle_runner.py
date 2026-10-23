"""계획 주기 한 번을 태스크로 실행한다: forecast 인스턴스 → 검증agent → supply_coordination → 라인별 정기 조율 (GRAPH_FLOW.md "동시성 모델").

- forecast 채널은 role_tag 하나를 N개 인스턴스가 공유한다. `ForecastInstances`는 인스턴스((회사, item))마다 인박스(`asyncio.Queue`)와 태스크를 하나씩
  만들고, 라우터 태스크 하나가 채널의 신호를 가리키는 기록으로 인스턴스를 찾아 그 인박스에 넣는다. 인스턴스 태스크는 인박스를 한 건씩 처리하므로
  인스턴스의 직전 결과(`ForecastInstanceRuntime.handling`)는 그 태스크만 만진다.
- 신호는 인스턴스 태스크가 처리를 끝낸 뒤에만 확인(`ack`)한다. 처리 중 예외가 나면 확인하지 않아 신호가 다시 나타나고, 인박스에 이미 있는 신호는 다시
  넣지 않는다. 가리키는 인스턴스가 없는 신호는 로그를 남기고 확인해 큐에서 뺀다.
- 인스턴스와 인박스는 라우터를 시작하기 전에 모두 만든다. 첫 실행도 신호와 같은 상태 판단(`handle_forecast_signal`)을 거친다(보류면 건너뛰고 로그,
  `dismiss`이면 실행하지 않음).
- 검증agent는 자기 채널을 처리하는 루프 태스크로 돈다. 처리 중 예외가 나면 확인하지 않는다.
- `run_planning_cycle`은 태스크를 만들고 주기를 시작하는 알림(`notify_cycle_started`)을 보낸 뒤, 모든 소비 채널에 처리할 신호가 없고 첫 실행이 끝날 때까지
  기다리고 태스크를 정리한다.
"""

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from .access import StateStore
from .external_data import load_instance_inputs
from .forecast_signals import ForecastInstanceRuntime, handle_forecast_signal
from .logging_utils import log
from .market_inputs import RAW_DIR, load_market_inputs
from .supply_coordination_agent import SupplyCoordinator, notify_cycle_started
from .validation_agent import ValidationRules, validate_job

ROLE_TAG = "forecast"
InstanceKey = tuple[str, str]  # (company_id, item_id)
IDLE_POLL_SECONDS = 0.01
CYCLE_TIMEOUT_SECONDS = 600.0  # 주기 하나를 기다리는 최대 시간(안전장치)


def sample_instance_runtimes(
    pairs: Sequence[InstanceKey], data_dir: str | Path | None = None, with_market: bool = True, raw_dir: Path = RAW_DIR
) -> dict[InstanceKey, ForecastInstanceRuntime]:
    """샘플 파일에서 인스턴스마다 forecast 실행 입력을 읽어 `ForecastInstanceRuntime`을 만든다.

    `with_market`(기본 참)이면 상품군의 시장 데이터(INA-R를 IPC로 보정, `market_inputs.py`)를 `data/raw`에서 읽어 넣는다. 원본이 없으면 FileNotFoundError다. 같은 상품군은 한 번만 읽는다.
    """
    runtimes: dict[InstanceKey, ForecastInstanceRuntime] = {}
    by_family: dict[str, tuple[pd.Series, pd.DataFrame] | None] = {}
    for company, item in pairs:
        inputs = load_instance_inputs(company, item, data_dir)
        if with_market:
            if inputs.family not in by_family:
                by_family[inputs.family] = load_market_inputs(inputs.family, inputs.data_end, raw_dir)
            market = by_family[inputs.family]
            if market is not None:
                inputs.market_index, inputs.market_group_index = market
        runtimes[company, item] = ForecastInstanceRuntime(inputs)
    return runtimes


class ForecastInstances:
    """forecast 채널의 라우터와 인스턴스별 인박스·태스크. 인스턴스와 인박스는 생성할 때 모두 만든다."""

    def __init__(self, store: StateStore, runtimes: Mapping[InstanceKey, ForecastInstanceRuntime], role_tag: str = ROLE_TAG) -> None:
        self.store = store
        self.role_tag = role_tag
        self.runtimes = dict(runtimes)
        keys = [(r.company_id, r.item_id) for r in store.get_field(role_tag, "forecast_records")]
        missing = [key for key in keys if key not in self.runtimes]
        if missing:
            raise ValueError(f"인스턴스의 실행 입력이 없음: {missing}")
        self.inboxes: dict[InstanceKey, asyncio.Queue[dict[str, Any]]] = {key: asyncio.Queue() for key in keys}
        self.first_runs_pending = len(keys)  # 첫 실행을 마치지 않은 인스턴스 수(첫 실행에는 신호가 없다)
        self._queued: set[int] = set()  # 인박스에 들어 있는 신호(객체 동일성)

    def key_of(self, message: Mapping[str, Any]) -> InstanceKey | None:
        """신호가 가리키는 기록의 인스턴스. 찾을 수 없으면 None이다."""
        path = message.get("record_path")
        if not isinstance(path, str) or "[" not in path:
            return None
        collection, index = path.split("[")[0], path[path.index("[") + 1 : -1]
        try:
            if collection == "forecast_records":
                record = self.store.get_field(self.role_tag, f"forecast_records[{int(index)}]")
                return (record.company_id, record.item_id)
            if collection == "escalation_records":
                agent_id = self.store.get_field(self.role_tag, f"escalation_records[{int(index)}]").agent_id
                return next(((r.company_id, r.item_id) for r in self.store.get_field(self.role_tag, "forecast_records") if r.agent_id == agent_id), None)
        except (IndexError, ValueError):
            return None
        return None

    async def run_router(self) -> None:
        """채널의 신호를 하나씩 꺼내 가리키는 인스턴스의 인박스에 넣는다. 확인은 인스턴스 태스크가 처리한 뒤에 한다."""
        queue = self.store.queue(self.role_tag)
        while True:
            message = await queue.get()
            key = self.key_of(message)
            if key is None or key not in self.inboxes:
                log(self.role_tag, "run_router", unknown_signal=message.get("record_path"), acked=True)
                queue.ack(message)
                continue
            if id(message) in self._queued:  # 확인이 늦어 다시 나타난 신호가 이미 인박스에 있다
                continue
            self._queued.add(id(message))
            self.inboxes[key].put_nowait(message)

    async def run_instance(self, key: InstanceKey) -> None:
        """인스턴스 태스크: 첫 실행을 한 뒤 인박스의 신호를 한 건씩 처리한다."""
        records = self.store.get_field(self.role_tag, "forecast_records")
        index = next(i for i, r in enumerate(records) if (r.company_id, r.item_id) == key)
        try:
            await self._process(key, {"record_path": f"forecast_records[{index}]"}, None)  # 첫 실행도 같은 상태 판단을 거친다
        finally:
            self.first_runs_pending -= 1
        while True:
            message = await self.inboxes[key].get()
            self._queued.discard(id(message))
            await self._process(key, message, message)

    async def _process(self, key: InstanceKey, message: Mapping[str, Any], signal: dict[str, Any] | None) -> None:
        try:
            outcome = await handle_forecast_signal(self.store, message, {key: self.runtimes[key]}, role_tag=self.role_tag)
        except Exception as error:  # 이 신호만 실패시키고 인스턴스는 계속 받는다. 확인하지 않아 신호가 다시 나타난다
            log(self.role_tag, "run_instance", company_id=key[0], item_id=key[1], skipped_ack=True, error=f"{type(error).__name__}: {error}")
            return
        if signal is not None:
            self.store.queue(self.role_tag).ack(signal)
        log(self.role_tag, "run_instance", company_id=key[0], item_id=key[1], outcome=outcome)


async def run_validator(store: StateStore, validator_role_tag: str, rules_by_author: Mapping[str, ValidationRules]) -> None:
    """검증agent 루프: 자기 채널의 신호를 하나씩 처리하고, 처리를 끝낸 신호만 확인한다."""
    queue = store.queue(validator_role_tag)
    while True:
        message = await queue.get()
        try:
            validate_job(store, validator_role_tag, message, rules_by_author)
        except Exception as error:
            log(validator_role_tag, "run_validator", skipped_ack=True, error=f"{type(error).__name__}: {error}")
            continue
        queue.ack(message)


def is_idle(store: StateStore, channels: Sequence[str], instances: ForecastInstances) -> bool:
    """처리할 신호(준비됐거나 처리 중인 것)가 소비 채널에 없고 첫 실행을 마치지 않은 인스턴스가 없는가."""
    return instances.first_runs_pending == 0 and all(
        store.queue(channel).qsize() == 0 and store.queue(channel).in_flight_count() == 0 for channel in channels
    )


async def run_planning_cycle(
    store: StateStore,
    coordinator: SupplyCoordinator,
    instances: ForecastInstances,
    validators: Mapping[str, Mapping[str, ValidationRules]],
    *,
    start_order: Sequence[InstanceKey] | None = None,
    timeout: float = CYCLE_TIMEOUT_SECONDS,
) -> None:
    """계획 주기 하나를 끝까지 실행한다. `validators`는 검증agent의 role_tag → 작성agent별 검증 규칙이다.

    `start_order`는 인스턴스 태스크를 만드는 순서다(기본은 forecast 기록 순서). 태스크 하나가 예외로 끝나면 그 예외를 올린다.
    """
    keys = list(start_order) if start_order is not None else list(instances.inboxes)
    consumers = [coordinator.role_tag, instances.role_tag, *validators]
    tasks = [asyncio.create_task(coordinator.run()), asyncio.create_task(instances.run_router())]
    tasks += [asyncio.create_task(run_validator(store, tag, rules)) for tag, rules in validators.items()]
    tasks += [asyncio.create_task(instances.run_instance(key)) for key in keys]
    notify_cycle_started(store, role_tag=coordinator.role_tag)
    try:
        async with asyncio.timeout(timeout):
            while True:
                failed = next((task for task in tasks if task.done() and not task.cancelled()), None)
                if failed is not None:
                    failed.result()  # 끝나지 않아야 할 태스크가 끝났다 — 예외를 올린다
                    raise RuntimeError("태스크가 끝났음")
                if is_idle(store, consumers, instances):
                    await asyncio.sleep(IDLE_POLL_SECONDS)  # 한 번 더 확인해 깨어나는 중인 태스크가 없는지 본다
                    if is_idle(store, consumers, instances):
                        return
                await asyncio.sleep(IDLE_POLL_SECONDS)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
