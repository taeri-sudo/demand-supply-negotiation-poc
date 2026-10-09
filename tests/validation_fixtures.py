"""forecast → 검증agent → supply_coordination 흐름을 구성하는 테스트 전용 도구.

[테스트 전용 입력] 과거 월별 요청량은 값 범위가 넓은 고정 목록이라, 가정 값이 범위를 벗어나는 검증은 일부러 좁은 목록을
넘기는 테스트가 따로 한다.
"""

from collections.abc import Mapping

from sop.access import FieldWrite, StateStore
from sop.agent_cards import default_agent_cards
from sop.forecast_human_manager import ForecastEscalationReason, new_forecast_escalation
from sop.forecast_supply_allocation import (
    forecast_supply_protocol_entry,
    forward_validated_forecast,
    process_pending_allocations,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG, forecast_validation_rules
from sop.state import AgentCard, AllocationCandidate, ForecastRecord, InteractionProtocol, RolePermission, State
from sop.validation_agent import ValidationRules, process_pending_validations

FORECAST = "forecast"
SUPPLY = "supply_coordination"
WIDE_HISTORY = [0.0, 200.0]
VERDICT_CHANNEL = FORECAST  # 채널은 agent의 role_tag 하나다

# (역할, field_path, 접근). 검증agent는 forecast 기록을 읽고 각 기록의 `validation`에만 쓴다
_PERMISSIONS: list[tuple[str, str, str]] = [
    (FORECAST, "forecast_records", "r"), (FORECAST, "forecast_records", "w"),
    (FORECAST, "escalation_records", "r"), (FORECAST, "escalation_records", "w"), (FORECAST, "agent_cards", "r"),
    (SUPPLY, "allocation_candidates", "r"), (SUPPLY, "allocation_candidates", "w"),
    (SUPPLY, "forecast_records", "r"), (SUPPLY, "escalation_records", "r"), (SUPPLY, "agent_cards", "r"),
    (SUPPLY, "forecast_records[*].send_back", "w"),
    (VALIDATOR_ROLE_TAG, "forecast_records", "r"),
    (VALIDATOR_ROLE_TAG, "forecast_records[*].validation", "w"),
    (VALIDATOR_ROLE_TAG, "escalation_records", "r"), (VALIDATOR_ROLE_TAG, "escalation_records", "w"),
    (VALIDATOR_ROLE_TAG, "interaction_protocol", "r"),
    # 로그: 각 역할은 자기 로그에만 쓰고, 모든 역할의 로그를 합쳐 읽을 수 있다
    *((role, "role_logs", "r") for role in (FORECAST, SUPPLY, VALIDATOR_ROLE_TAG)),
    *((role, f"role_logs.{role}", "w") for role in (FORECAST, SUPPLY, VALIDATOR_ROLE_TAG)),
]


def permissions(extra: tuple[RolePermission, ...] = ()) -> list[RolePermission]:
    result = [
        RolePermission(role_tag=role, field_path=field, access=access)  # pyright: ignore[reportArgumentType]
        for role, field, access in _PERMISSIONS
    ]
    return [*result, *extra]


def make_store(
    records: list[ForecastRecord],
    protocol: list[InteractionProtocol] | None = None,
    extra_permissions: tuple[RolePermission, ...] = (),
    cards: list[AgentCard] | None = None,
) -> StateStore:
    entries = [forecast_supply_protocol_entry()] if protocol is None else protocol
    return StateStore(
        State(
            forecast_records=records, interaction_protocol=entries, role_permissions=permissions(extra_permissions),
            agent_cards=default_agent_cards() if cards is None else cards,
        )
    )


def wide_history(company_id: str, item_id: str) -> list[float]:
    return WIDE_HISTORY


def forecast_rules(history_of=wide_history) -> Mapping[str, ValidationRules]:
    return {FORECAST: forecast_validation_rules(history_of)}


def take_verdict_signals(store: StateStore) -> list[dict]:
    """`forecast` 채널에 도착한 신호를 모두 꺼낸다."""
    queue = store.queue(VERDICT_CHANNEL)
    signals = []
    while not queue.empty():
        signals.append(queue.get_nowait())
    return signals


def forward_passed(store: StateStore) -> int:
    """도착한 판정 신호마다 forecast가 신호가 가리키는 자기 기록의 판정을 확인해 `passed`인 것을 supply_coordination에게 보낸다."""
    sent = 0
    for signal in take_verdict_signals(store):
        record = store.get_field(FORECAST, signal["record_path"])
        sent += forward_validated_forecast(store, FORECAST, record.company_id, record.item_id)
    return sent


def validate_and_allocate(store: StateStore, rules: Mapping[str, ValidationRules] | None = None) -> list[AllocationCandidate]:
    """검증agent 큐를 모두 판정하고, forecast가 통과한 기록을 보내면 supply_coordination이 모두 처리한다."""
    process_pending_validations(store, VALIDATOR_ROLE_TAG, rules if rules is not None else forecast_rules())
    forward_passed(store)
    return process_pending_allocations(store, SUPPLY)


def write_verdict(store: StateStore, role_tag: str, record_path: str, result) -> None:
    """레코드의 `validation`에 판정을 쓴다(검증agent의 판정 기록과 같은 형태). 신호는 기록의 상태가 바뀔 때 자동으로 간다."""
    store.update_state(role_tag, fields=[FieldWrite(f"{record_path}.validation", result)])


def open_hold(store: StateStore, agent_id: str, reason: ForecastEscalationReason = "options_exhausted", rationale="테스트"):
    """인스턴스를 보류 상태로 만든다(처리되지 않은 intervention escalation 기록을 하나 더한다). 기록의 상태가 "사람 대기"가 된다."""
    escalation = new_forecast_escalation(agent_id, reason, rationale)
    store.update_state(FORECAST, fields=[FieldWrite("escalation_records", [*store.state.escalation_records, escalation])])
    return escalation
