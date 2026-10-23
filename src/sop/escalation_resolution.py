"""사람의 escalation 처리 (STATE_SCHEMA.md 7번 절 "사람의 처리", GRAPH_FLOW.md "사람 입력과 검증의 무결성").

사람이 State에 값을 쓰는 경로는 `resolve_escalation` 하나뿐이다. escalation 기록(`status`·`resolution`)과 값을 받는 기록, 로그 항목
(`resolution_applied`)을 `update_state` 한 번으로 쓴다.

- `human_manager`(업무 판단)는 `kind: "value"`로 값을 넣는다. forecast의 reason(`no_computable_assumption`, `selection_unresolved`,
  `options_exhausted`)은 요청량(숫자) 하나이고, `commitments_exceed_capacity`는 고객사별 하한 수량(`{demand_ref: quantity}`)이다.
  forecast 기록에는 `scenario`(`value`는 넣은 요청량, `assumption_ids`는 빈 목록, `derivation`은 null — 기존 `scenario`가 있든 없든 같다)와
  `selection_basis: "human"`을 쓰고 판정 칸(`validation`)·`forward_to`·`send_back`은 비운다 — 판정 칸에는 검증agent의 판정만 쓰므로 기록이
  검증 면제 통과(`exempt_passed`)가 되어 검증agent를 다시 거치지 않고 forecast가 다음 agent로 넘긴다. 덮어쓰기 전 `scenario`는 로그
  `resolution_applied`의 payload에 남는다. `commitments_exceed_capacity`의 하한 수량과 `pegged_exceed_capacity`의 유지할 강한 묶임 수량은 기록에
  직접 쓰지 않고 `resolution`에 남아 다음 조율의 입력이 된다.
- `system_operator`(시스템 오류)는 `kind: "rerun"`(실패한 실행을 다시 한다 — `forecast_run_error`는 forecast 실행, `validation_error`는 검증)이나
  `"dismiss"`(이번 주기에서 그 인스턴스를 뺌)를 넣는다.
  `validation_error`의 `rerun`은 판정 칸을 비워 기록이 다시 "검증 대기"가 되게 한다. 다른 reason의 `rerun`은 처리 상태만 바뀌고 기록의
  상태가 다시 계산돼 담당 agent에게 신호가 간다(예: `no_next_agent` 처리 뒤 forecast가 다음 agent를 다시 고른다).

넣는 값은 넣을 때 입력 제한으로 확인한다: 숫자이고 0 이상, 계약 단위의 배수(거부), 같은 입력의 중복(거부), 과거 월별 요청량 범위를 크게 벗어나면
경고(적용하고 `resolution.note`와 로그에 남긴다). agent 경계를 넘는 제약(공급 가능량 등)은 값이 배분으로 들어간 뒤 배분 결론의 검증에서 확인된다.

담당별 읽기 범위는 행 단위라 `role_permissions`가 아니라 `read_escalation_records`가 `target_role`로 거른다.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .access import FieldWrite, LogWrite, PermissionDenied, StateStore
from .escalation_records import OPEN_STATUS
from .judgment_thresholds import HUMAN_VALUE_RANGE_FACTOR
from .logging_utils import log
from .state import EscalationRecord, Resolution, ResolutionKind, Scenario

HUMAN_MANAGER = "human_manager"
SYSTEM_OPERATOR = "system_operator"
FORECAST_VALUE_REASONS = frozenset(["no_computable_assumption", "selection_unresolved", "options_exhausted"])
# 라인의 reason: 값은 {demand_ref: quantity}다 — 하한 수량(`commitments_exceed_capacity`) 또는 유지할 강한 묶임 수량(`pegged_exceed_capacity`)
QUANTITY_MAP_REASONS = frozenset(["commitments_exceed_capacity", "pegged_exceed_capacity"])

UnitOf = Callable[[str | None, str], float]  # (company_id, item_id) -> 계약 단위
HistoryOf = Callable[[str, str], list[float]]  # (company_id, item_id) -> 과거 월별 요청량


class ResolutionRejected(ValueError):
    """사람의 처리를 거부함. `code`는 거부 이유(`wrong_role`, `not_open`, `duplicate`, `unsupported`, `wrong_kind`, `not_a_number`, `negative`,
    `not_unit_multiple`, `empty`)다."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class ResolutionOutcome:
    escalation: EscalationRecord  # 처리한 뒤의 escalation 기록
    warnings: list[str]  # 적용했지만 알리는 경고(과거 범위를 크게 벗어남)


def read_escalation_records(store: StateStore, reader_role_tag: str) -> list[tuple[int, EscalationRecord]]:
    """사람이 읽는 escalation 기록(기록 번호, 기록). `human_manager`는 자기 담당(업무 판단) 기록만, `system_operator`는 전부 읽는다."""
    records = store.get_field(reader_role_tag, "escalation_records")
    if reader_role_tag == HUMAN_MANAGER:
        return [(i, r) for i, r in enumerate(records) if r.target_role == HUMAN_MANAGER]
    if reader_role_tag == SYSTEM_OPERATOR:
        return list(enumerate(records))
    raise PermissionDenied(f"{reader_role_tag}는 사람이 아니라 담당별로 거른 escalation 기록을 읽을 수 없음")


def _check_quantity(value: Any, unit: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ResolutionRejected("not_a_number", f"{label}: 숫자가 아님 ({value!r})")
    if value < 0:
        raise ResolutionRejected("negative", f"{label}: 0 이상이어야 함 ({value})")
    if abs(round(value / unit) * unit - value) > 1e-9:
        raise ResolutionRejected("not_unit_multiple", f"{label}: 계약 단위 {unit:g}의 배수가 아님 ({value})")
    return float(value)


def _range_warning(value: float, history: list[float]) -> str | None:
    """값이 과거 월별 요청량의 최대의 `HUMAN_VALUE_RANGE_FACTOR`배를 넘거나 최소의 그 배수 분의 1 아래면 경고 문구."""
    if not history:
        return None
    high, low = max(history), min(history)
    if high > 0 and value > high * HUMAN_VALUE_RANGE_FACTOR:
        return f"과거 월별 요청량의 최대 {high:g}의 {HUMAN_VALUE_RANGE_FACTOR:g}배를 넘음 ({value:g})"
    if low > 0 and value < low / HUMAN_VALUE_RANGE_FACTOR:
        return f"과거 월별 요청량의 최소 {low:g}의 1/{HUMAN_VALUE_RANGE_FACTOR:g} 아래임 ({value:g})"
    return None


def _forecast_index(store: StateStore, role_tag: str, agent_id: str) -> int:
    for index, record in enumerate(store.get_field(role_tag, "forecast_records")):
        if record.agent_id == agent_id:
            return index
    raise ValueError(f"forecast_records에 {agent_id!r} 인스턴스가 없음")


def _allocation_index(store: StateStore, role_tag: str, line_id: str) -> int:
    for index, record in enumerate(store.get_field(role_tag, "allocations")):
        if record.line_id == line_id:
            return index
    raise ValueError(f"allocations에 라인 {line_id!r}의 기록이 없음")


def resolve_escalation(
    store: StateStore,
    resolver_role_tag: str,
    index: int,
    *,
    kind: ResolutionKind,
    value: Any = None,
    note: str | None = None,
    unit_of: UnitOf | None = None,
    history_of: HistoryOf | None = None,
    floor_unit_of: Callable[[str], float] | None = None,
    now: datetime | None = None,
) -> ResolutionOutcome:
    """`index`번 escalation 기록(intervention)을 처리한다. 거부하면 `ResolutionRejected`이고 아무것도 쓰지 않는다.

    `unit_of`·`history_of`는 forecast reason의 요청량을 확인할 때, `floor_unit_of`는 하한 수량(`{demand_ref: quantity}`)을 확인할 때 쓴다.
    """
    records = store.get_field(resolver_role_tag, "escalation_records")
    if not 0 <= index < len(records):
        raise ValueError(f"escalation_records[{index}]가 없음")
    escalation = records[index]
    if escalation.mode != "intervention":
        raise ResolutionRejected("unsupported", "notice는 사람의 결정이 없는 알림이라 처리할 수 없음")
    if escalation.target_role != resolver_role_tag:
        raise ResolutionRejected("wrong_role", f"{escalation.reason}는 {escalation.target_role} 담당임")
    if escalation.status != OPEN_STATUS:
        same = escalation.resolution is not None and (escalation.resolution.kind, escalation.resolution.value) == (kind, value)
        raise ResolutionRejected("duplicate" if same else "not_open", f"이미 {escalation.status}인 escalation 기록")

    warnings: list[str] = []
    fields: list[FieldWrite] = []
    previous_scenario: dict | None = None
    if resolver_role_tag == HUMAN_MANAGER:
        if kind != "value":
            raise ResolutionRejected("wrong_kind", "human_manager는 kind가 value인 처리만 한다")
        if escalation.reason in FORECAST_VALUE_REASONS:
            quantity, warning = _check_forecast_value(store, resolver_role_tag, escalation, value, unit_of, history_of)
            warnings += [warning] if warning else []
            value_writes, previous_scenario = _forecast_value_writes(store, resolver_role_tag, escalation, quantity)
            fields += value_writes
        elif escalation.reason in QUANTITY_MAP_REASONS:
            _check_quantity_map(value, floor_unit_of)
        else:
            raise ResolutionRejected("unsupported", f"{escalation.reason}는 사람이 값을 넣어 처리하는 reason이 아님")
    else:
        if kind == "value":
            raise ResolutionRejected("wrong_kind", "system_operator는 rerun이나 dismiss만 한다")
        if value is not None:
            raise ResolutionRejected("wrong_kind", "rerun·dismiss에는 값이 없다")
        if escalation.reason == "validation_error" and kind == "rerun":
            fields.append(_clear_verdict(store, resolver_role_tag, escalation))  # 기록이 다시 "검증 대기"가 된다

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    resolution = Resolution(
        resolved_by=resolver_role_tag, ts=stamp, kind=kind, value=value, note="; ".join(filter(None, [note, *warnings])) or None
    )
    resolved = escalation.model_copy(update={"status": "resolved", "resolution": resolution})
    fields.insert(0, FieldWrite(f"escalation_records[{index}]", resolved))
    store.update_state(
        resolver_role_tag,
        fields=fields,
        logs=[
            LogWrite(
                "resolution_applied", escalation.agent_id,
                payload={
                    "escalation": resolved.model_dump(mode="json"), "value": value, "warnings": warnings, "line_id": escalation.line_id,
                    "previous_scenario": previous_scenario,
                },
            )
        ],
    )
    log(resolver_role_tag, "resolve_escalation", reason=escalation.reason, kind=kind, warnings=len(warnings))
    return ResolutionOutcome(resolved, warnings)


def _check_forecast_value(
    store: StateStore, role_tag: str, escalation: EscalationRecord, value: Any, unit_of: UnitOf | None, history_of: HistoryOf | None
) -> tuple[float, str | None]:
    if escalation.agent_id is None:
        raise ValueError("forecast reason의 escalation 기록에는 agent_id가 있어야 함")
    if unit_of is None:
        raise ValueError("forecast 요청량을 확인하려면 unit_of가 필요함")
    record = store.get_field(role_tag, f"forecast_records[{_forecast_index(store, role_tag, escalation.agent_id)}]")
    quantity = _check_quantity(value, unit_of(record.company_id, record.item_id), "요청량")
    history = history_of(record.company_id, record.item_id) if history_of is not None else []
    return quantity, _range_warning(quantity, history)


def _check_quantity_map(value: Any, floor_unit_of: Callable[[str], float] | None) -> None:
    if not isinstance(value, Mapping) or not value:
        raise ResolutionRejected("empty", "수량은 {demand_ref: quantity} 형태의 비어 있지 않은 값이어야 함")
    if floor_unit_of is None:
        raise ValueError("수량을 확인하려면 floor_unit_of가 필요함")
    for demand_ref, quantity in value.items():
        _check_quantity(quantity, floor_unit_of(demand_ref), f"수량 {demand_ref}")


def _forecast_value_writes(
    store: StateStore, role_tag: str, escalation: EscalationRecord, quantity: float
) -> tuple[list[FieldWrite], dict | None]:
    """사람이 넣은 요청량을 `scenario`로 쓰는 필드 쓰기와, 덮어쓰기 전 `scenario`(없었으면 None)."""
    assert escalation.agent_id is not None
    path = f"forecast_records[{_forecast_index(store, role_tag, escalation.agent_id)}]"
    record = store.get_field(role_tag, path)
    scenario = Scenario(value=quantity, assumption_ids=[], derivation=None)  # 가정에서 만든 값이 아니다
    previous = record.scenario.model_dump(mode="json") if record.scenario is not None else None
    return [
        FieldWrite(f"{path}.scenario", scenario),
        FieldWrite(f"{path}.selection_basis", "human"),
        FieldWrite(f"{path}.validation", None),  # 판정 칸에는 검증agent의 판정만 쓴다
        FieldWrite(f"{path}.forward_to", None),
        FieldWrite(f"{path}.send_back", None),
    ], previous


def _clear_verdict(store: StateStore, role_tag: str, escalation: EscalationRecord) -> FieldWrite:
    if escalation.agent_id is not None:
        return FieldWrite(f"forecast_records[{_forecast_index(store, role_tag, escalation.agent_id)}].validation", None)
    if escalation.line_id is not None:
        return FieldWrite(f"allocations[{_allocation_index(store, role_tag, escalation.line_id)}].validation", None)
    raise ValueError("validation_error의 escalation 기록에는 agent_id나 line_id가 있어야 함")
