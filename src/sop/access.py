"""모든 State 읽기/쓰기가 거쳐야 하는 접근통제 wrapper.

State에 직접 접근하지 않고 get_field/set_field만 거치게 강제한다(CLAUDE.md).
set_field는 값 기록과 큐 push를 항상 짝짓는다 — 큐는 "초인종" 역할일 뿐,
실제 기록은 State/negotiation_log가 담당한다(STATE_SCHEMA.md "동시성 모델").
"""

import asyncio
import re
from collections import defaultdict
from typing import Any

from .logging_utils import log
from .state import State

_SEGMENT_RE = re.compile(r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?:\[(?P<index>\d+)\])?$")


class PermissionDenied(PermissionError):
    """role_permissions 화이트리스트에 없는 접근."""


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


def _is_permitted(role_permissions: list, role_tag: str, field_path: str, access: str) -> bool:
    for perm in role_permissions:
        if perm.role_tag != role_tag or perm.access != access:
            continue
        if perm.field_path == "*":
            return True
        if (
            field_path == perm.field_path
            or field_path.startswith(perm.field_path + ".")
            or field_path.startswith(perm.field_path + "[")
        ):
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
        """channel(role_tag 또는 "validation_result.{role_tag}" 같은 세부 채널)의 큐."""
        return self._queues[channel]

    def lock(self, name: str) -> asyncio.Lock:
        """공유자원(capacity_pools 등) 보호용 Lock. 이름으로 자원을 구분."""
        return self._locks[name]

    def get_field(self, role_tag: str, field_path: str) -> Any:
        if not _is_permitted(self._state.role_permissions, role_tag, field_path, "r"):
            raise PermissionDenied(f"{role_tag}는 {field_path}를 읽을 권한이 없음")
        return _resolve_read(self._state, _parse_path(field_path))

    def set_field(self, role_tag: str, field_path: str, value: Any, *, notify_channel: str) -> None:
        if not _is_permitted(self._state.role_permissions, role_tag, field_path, "w"):
            raise PermissionDenied(f"{role_tag}는 {field_path}에 쓸 권한이 없음")
        self._state = _rebuild(self._state, _parse_path(field_path), value)
        self._queues[notify_channel].put_nowait({"field_path": field_path, "written_by": role_tag})
        log(role_tag, "set_field", field_path=field_path, notify_channel=notify_channel)
