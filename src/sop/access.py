"""모든 State 읽기/쓰기가 거쳐야 하는 접근통제 wrapper.

State에 직접 접근하지 않고 get_field/set_field/update_state만 거치게 강제한다(CLAUDE.md).
실제 기록은 State/role_logs가 담당하고, 큐는 "초인종" 역할일 뿐이다(STATE_SCHEMA.md "동시성 모델").

쓰는 쪽은 신호를 지정하지 않는다. `update_state`가 State를 갱신한 뒤 기록의 상태가 바뀐 곳에만, 바뀐 상태의 담당에게 자동으로 신호를
보낸다(`record_state.py`, GRAPH_FLOW.md "신호 규칙"). 상태를 바꾸지 않는 쓰기(로그 등)는 신호를 보내지 않는다.
"""

import asyncio
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .escalation_records import OPEN_STATUS, has_open_intervention
from .logging_utils import log
from .ids import now_iso
from .agent_cards import card_of
from .record_state import VALIDATOR_OF_COLLECTION, owner_channel, record_state
from .state import EscalationRecord, LogEntry, LogEvent, State, merge_role_logs

_SEGMENT_RE = re.compile(r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\[(?P<index>\d+)\])?$")
# 권한의 field_path에서는 인스턴스 인덱스 자리에 와일드카드 `[*]`를 쓸 수 있다
_PERMISSION_SEGMENT_RE = re.compile(r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\[(?P<index>\d+|\*)\])?$")


@dataclass(frozen=True)
class FieldWrite:
    """`update_state`이 State 한 곳에 쓰는 값."""

    field_path: str
    value: Any


@dataclass(frozen=True)
class LogWrite:
    """`update_state`이 쓰는 로그 항목. `seq`·`ts`·`role_tag`는 `update_state`이 붙인다."""

    event: LogEvent
    agent_id: str | None = None
    round: int | None = None
    payload: dict = field(default_factory=dict)


class PermissionDenied(PermissionError):
    """role_permissions 화이트리스트에 없는 접근."""


class ForwardTargetRejected(ValueError):
    """`forward_to`가 작성agent 카드의 `known_agents` 밖이라 거부됨."""


class FieldPathError(ValueError):
    """field_path 문법 오류."""


def _parse_path(field_path: str) -> list[tuple[str, int | None]]:
    segments = []
    for raw in field_path.split("."):
        match = _SEGMENT_RE.match(raw)
        if not match:
            raise FieldPathError(f"잘못된 field_path 세그먼트: {raw!r}")
        index = match.group("index")
        segments.append((match.group("name"), int(index) if index is not None else None))
    return segments


def _resolve_read(root: Any, segments: list[tuple[str, int | None]]) -> Any:
    value = root
    for name, index in segments:
        value = getattr(value, name)
        if index is not None:
            value = value[index]
    return value


def _rebuild(root: Any, segments: list[tuple[str, int | None]], value: Any) -> Any:
    """segments 경로 끝에 value를 쓴 root의 복사본을 반환(model_copy 기반, 원본 불변)."""
    name, index = segments[0]
    rest = segments[1:]
    current = getattr(root, name)
    if index is None:
        new_child = value if not rest else _rebuild(current, rest, value)
        return root.model_copy(update={name: new_child})
    new_list = list(current)
    new_list[index] = value if not rest else _rebuild(new_list[index], rest, value)
    return root.model_copy(update={name: new_list})


def _parse_permission_path(field_path: str) -> list[tuple[str, int | str | None]]:
    segments: list[tuple[str, int | str | None]] = []
    for raw in field_path.split("."):
        match = _PERMISSION_SEGMENT_RE.match(raw)
        if not match:
            raise FieldPathError(f"잘못된 권한 field_path 세그먼트: {raw!r}")
        index = match.group("index")
        segments.append((match.group("name"), int(index) if index is not None and index != "*" else index))
    return segments


def _path_is_covered(permission_path: str, field_path: str) -> bool:
    """권한 경로가 접근 경로를 포함하는가. 권한 경로의 인덱스가 없으면(마지막 세그먼트) 모든 인덱스와 그 아래 필드를 포함하고,
    `[*]`는 모든 인스턴스 인덱스를 뜻하며, 숫자는 그 인덱스만 뜻한다. 접근 경로가 권한 경로보다 짧으면 포함하지 않는다."""
    permitted = _parse_permission_path(permission_path)
    accessed = _parse_path(field_path)
    if len(permitted) > len(accessed):
        return False
    for position, ((p_name, p_index), (name, index)) in enumerate(zip(permitted, accessed)):
        if p_name != name:
            return False
        if p_index is None:
            if position < len(permitted) - 1 and index is not None:
                return False
        elif p_index == "*":
            if index is None:
                return False
        elif p_index != index:
            return False
    return True


def _is_permitted(role_permissions: list, role_tag: str, field_path: str, access: str) -> bool:
    for perm in role_permissions:
        if perm.role_tag != role_tag or perm.access != access:
            continue
        if perm.field_path == "*" or _path_is_covered(perm.field_path, field_path):
            return True
    return False


class StateStore:
    """State 하나와, 그 State에 대한 유일한 접근 경로(get_field/set_field)를 갖는다."""

    def __init__(self, initial_state: State) -> None:
        self._state = initial_state
        self._queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @property
    def state(self) -> State:
        return self._state

    def queue(self, channel: str) -> asyncio.Queue:
        """agent의 채널(그 agent의 role_tag)의 큐."""
        return self._queues[channel]

    def lock(self, name: str) -> asyncio.Lock:
        """공유자원(capacity_pools 등) 보호용 Lock. 이름으로 자원을 구분."""
        return self._locks[name]

    def get_field(self, role_tag: str, field_path: str) -> Any:
        if not _is_permitted(self._state.role_permissions, role_tag, field_path, "r"):
            raise PermissionDenied(f"{role_tag}는 {field_path}를 읽을 권한이 없음")
        return _resolve_read(self._state, _parse_path(field_path))

    def _record_states(self, state: State) -> dict[str, tuple[str, Any]]:
        """상태를 가진 모든 기록의 (경로 → (상태, 기록))."""
        states: dict[str, tuple[str, Any]] = {}
        for collection in VALIDATOR_OF_COLLECTION:
            for index, record in enumerate(getattr(state, collection)):
                states[f"{collection}[{index}]"] = (record_state(record, state.escalation_records), record)
        return states

    def update_state(
        self,
        role_tag: str,
        *,
        fields: Sequence[FieldWrite] = (),
        logs: Sequence[LogWrite] = (),
    ) -> list[LogEntry]:
        """여러 필드와 로그 항목을 **한 번의 State 갱신**으로 쓴다. 전부 쓰거나 하나도 쓰지 않는다.

        - 쓸 곳마다 `role_permissions`의 쓰기 권한을 먼저 모두 확인한다. 하나라도 없으면 아무것도 쓰지 않는다.
        - 새 State를 모두 계산한 뒤 한 번에 교체한다. 계산 중 예외가 나면 State는 이전 그대로이고 신호도 가지 않으며, 예외는
          삼키지 않고 그대로 올라간다.
        - 로그 항목은 역할 자신의 로그(`role_logs.{role_tag}`)에 `next_log_seq()`부터 차례로 붙는다.
        - 신호는 갱신이 끝난 뒤 자동으로 보낸다: 기록의 상태가 바뀐 곳에만, 바뀐 상태의 담당에게 보낸다(검증 대기는 검증agent,
          판정됨과 되돌려짐은 작성agent, 전달됨은 `forward_to`의 agent, 사람 대기는 human_manager. 채널은 담당 agent의 role_tag).
          호출하는 쪽은 신호를 지정하지 않는다.
        - `forward_to`가 바뀌면 작성agent 카드의 `known_agents`에 있는 role_tag만 허용한다. 아니면 `ForwardTargetRejected`를 내고 아무것도 쓰지 않는다.
        """
        log_path = f"role_logs.{role_tag}"
        needed = [write.field_path for write in fields] + ([log_path] if logs else [])
        for path in needed:
            if not _is_permitted(self._state.role_permissions, role_tag, path, "w"):
                raise PermissionDenied(f"{role_tag}는 {path}에 쓸 권한이 없음")
        updated = self._state
        for write in fields:
            updated = _rebuild(updated, _parse_path(write.field_path), write.value)
        first_seq = self.next_log_seq()
        entries = [
            LogEntry(
                seq=first_seq + offset, ts=now_iso(), role_tag=role_tag, agent_id=item.agent_id, event=item.event,
                round=item.round, payload=item.payload,
            )
            for offset, item in enumerate(logs)
        ]
        if entries:
            own = updated.role_logs.get(role_tag, [])
            updated = updated.model_copy(update={"role_logs": {**updated.role_logs, role_tag: [*own, *entries]}})
        self._check_forward_targets(self._state, updated)
        before = self._record_states(self._state)
        after = self._record_states(updated)
        self._state = updated  # 한 번의 State 갱신
        sent: list[str] = []
        for path, (new_state, record) in after.items():
            old_state = before.get(path, ("none", None))[0]
            if new_state != old_state and new_state != "none":
                sent.append(self._signal_for_state(role_tag, path, new_state, record))
        log(role_tag, "update_state", fields=[w.field_path for w in fields], log_seqs=[e.seq for e in entries], signals=sent)
        return entries

    def _check_forward_targets(self, before: State, after: State) -> None:
        """새로 쓰는 `forward_to`가 작성agent 카드의 `known_agents`에 있는지 확인한다."""
        for collection in VALIDATOR_OF_COLLECTION:
            old_records = getattr(before, collection)
            for index, record in enumerate(getattr(after, collection)):
                old = old_records[index] if index < len(old_records) else None
                if record.forward_to is None or (old is not None and old.forward_to == record.forward_to):
                    continue
                card = card_of(after.agent_cards, record.role_tag)
                if card is None or record.forward_to not in card.known_agents:
                    raise ForwardTargetRejected(f"{record.role_tag}는 {record.forward_to}에게 넘길 수 없음(known_agents 밖)")

    def _signal_for_state(self, role_tag: str, record_path: str, state: str, record: Any) -> str:
        """기록이 `state`가 됐다는 신호를 그 상태의 담당 agent의 채널로 보내고 채널 이름을 반환한다."""
        collection = record_path.split("[")[0]
        channel = owner_channel(state, record, VALIDATOR_OF_COLLECTION[collection])  # pyright: ignore[reportArgumentType]
        self._queues[channel].put_nowait({"field_path": record_path, "written_by": role_tag, "record_path": record_path})
        return channel

    def set_field(self, role_tag: str, field_path: str, value: Any) -> None:
        """필드 하나를 쓴다. `update_state`와 같다(신호는 기록의 상태가 바뀔 때 자동으로 간다)."""
        self.update_state(role_tag, fields=[FieldWrite(field_path, value)])

    def next_log_seq(self) -> int:
        """다음에 쓸 로그 항목의 `seq`. 보류에 들어갈 때 escalation 기록의 `log_seq`를 먼저 정하는 데 쓴다."""
        return 1 + max((entry.seq for entries in self._state.role_logs.values() for entry in entries), default=0)

    def write_held(
        self,
        role_tag: str,
        *,
        agent_id: str,
        escalation: EscalationRecord,
        log_event: LogEvent,
        log_payload: dict,
        extra_logs: Sequence[LogWrite] = (),
        field_path: str | None = None,
        value: Any = None,
    ) -> LogEntry:
        """보류에 들어갈 때 escalation 기록, 로그 항목, forecast 기록(`field_path`를 주면)을 `update_state`로 한 번에 쓴다.

        - `extra_logs`는 같은 갱신에서 먼저 쓸 로그 항목이다(예: 재실행 항목). `escalation.log_seq`는 먼저 정해 둔
          `next_log_seq() + len(extra_logs)`, 곧 `log_event` 항목의 seq여야 한다.
        - 기록은 "사람 대기" 상태가 되어 human_manager에게 신호가 가고 검증agent에게는 가지 않는다.
        - 이미 보류 중인 인스턴스에는 쓰지 않는다. 호출하기 전에 실행을 건너뛴다.
        """
        records = self._state.escalation_records
        if has_open_intervention(records, agent_id):
            raise ValueError(f"{agent_id}는 이미 보류 중이라 보류 쓰기를 할 수 없음")
        if escalation.agent_id != agent_id or escalation.mode != "intervention" or escalation.status != OPEN_STATUS:
            raise ValueError("보류 쓰기의 escalation 기록은 이 인스턴스의 처리되지 않은 intervention이어야 함")
        seq = self.next_log_seq() + len(extra_logs)
        if escalation.log_seq != seq:
            raise ValueError(f"escalation.log_seq({escalation.log_seq})가 쓸 로그 항목의 seq({seq})와 다름")
        fields = [FieldWrite("escalation_records", [*records, escalation])]
        if field_path is not None:
            fields.insert(0, FieldWrite(field_path, value))
        entries = self.update_state(
            role_tag, fields=fields, logs=[*extra_logs, LogWrite(log_event, agent_id, payload=log_payload)]
        )
        return entries[-1]

    def append_log(
        self,
        role_tag: str,
        event: LogEvent,
        *,
        agent_id: str | None = None,
        round: int | None = None,
        payload: dict | None = None,
    ) -> LogEntry:
        """역할 자신의 로그(`role_logs.{role_tag}`)에 항목 하나만 붙인다. 상태를 바꾸지 않으므로 신호가 가지 않는다."""
        (entry,) = self.update_state(role_tag, logs=[LogWrite(event, agent_id, round, payload or {})])
        return entry

    def role_log(self, reader_role_tag: str, owner_role_tag: str) -> list[LogEntry]:
        """한 역할의 로그를 읽는다(`role_logs.{owner}` 읽기 권한)."""
        field_path = f"role_logs.{owner_role_tag}"
        if not _is_permitted(self._state.role_permissions, reader_role_tag, field_path, "r"):
            raise PermissionDenied(f"{reader_role_tag}는 {field_path}를 읽을 권한이 없음")
        return list(self._state.role_logs.get(owner_role_tag, []))

    def negotiation_log(self, reader_role_tag: str) -> list[LogEntry]:
        """모든 역할의 로그를 `seq` 순으로 합쳐 읽는다(`role_logs` 읽기 권한). 합친 결과는 저장하지 않는다."""
        if not _is_permitted(self._state.role_permissions, reader_role_tag, "role_logs", "r"):
            raise PermissionDenied(f"{reader_role_tag}는 role_logs를 읽을 권한이 없음")
        return merge_role_logs(self._state.role_logs)
