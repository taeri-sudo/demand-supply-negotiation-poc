"""forecast agent ↔ supply_coordination agent 라운드 협상 루프 (M1).

GRAPH_FLOW.md "라운드 누적형" 상호작용을 구현하되, 이번 마일스톤은 회사
1개·capacity pool 1개로 범위를 좁힌다:

- supply_coordination 응답은 배분 대안이 없어(회사가 1개뿐) 판단이 아니라
  순수 함수(`supply_coordination_respond`) — capacity_pools를 건드리지 않고
  읽기만 한다(실제 배분 확정은 이후 마일스톤에서 별도로 처리).
- forecast의 다음 제안은 격차의 50%만 좁히는 규칙으로 계산하되, 공통 규칙 2에
  따라 `StructuredJudgment`(judgment/.py)로 감싼다(`forecast_next_proposal`).
  지금은 규칙 계산이지만 이후 마일스톤에서 실제 LLM 판단으로 교체될 수 있어
  함수를 async로 둔다.
- 수렴조건은 GRAPH_FLOW.md 원래 정의(변화폭 임계치 + forecast_reliability
  신뢰도 게이트)의 축소판 — forecast_reliability는 아직 영속화가 없어
  (STATE_SCHEMA.md 참고), 이번 마일스톤은 proposed 변화율 < 10%만 본다.
- `run_forecast_select_and_round`가 analysis 스텁(candidate) → forecast 후보
  선택 판단(`forecast_candidate_selection.py`) → 이 협상 루프까지 잇는
  상위 진입점이다.
"""

from .access import StateStore
from .forecast_candidate_selection import select_forecast_candidate
from .ids import now_iso
from .judgment import StructuredJudgment
from .state import (
    EscalationRecord,
    ForecastCandidate,
    ForecastRound,
    InteractionProtocol,
    NegotiationLogEntry,
)

CONVERGENCE_THRESHOLD = 0.10
COUNTER_STEP_RATIO = 0.5
EDGE = "forecast<->supply_coordination"


def supply_coordination_respond(remaining_capacity: float, proposed: float) -> dict:
    """remaining_capacity와 proposed만으로 accepted/counter를 정하는 함수.

    회사가 1개뿐이라 배분 우선순위를 매길 대안이 없어, 이번 마일스톤에서는
    supply_coordination의 agent 판단이 아니라 순수 함수로 둔다.
    """
    if remaining_capacity >= proposed:
        return {"status": "accepted", "quantity": proposed}
    return {"status": "counter", "quantity": remaining_capacity}


async def forecast_next_proposal(
    previous_proposed: float, response_quantity: float
) -> StructuredJudgment:
    """counter를 받은 뒤 격차의 50%만 좁혀 다음 제안을 정하는 forecast 판단."""
    gap = previous_proposed - response_quantity
    next_proposed = previous_proposed - COUNTER_STEP_RATIO * gap
    return StructuredJudgment(
        judgment={"next_proposed": next_proposed},
        reasoning=(
            f"counter 응답({response_quantity})과 이전 제안({previous_proposed})의 "
            f"격차({gap})를 절반만 좁혀 {next_proposed}로 재제안"
        ),
    )


def _find_protocol(store: StateStore, role_tag: str, edge: str) -> InteractionProtocol:
    protocols = store.get_field(role_tag, "interaction_protocol")
    return next(p for p in protocols if p.edge == edge)


def _append_log(store: StateStore, role_tag: str, event: str, round_num: int | None) -> None:
    log_entries = store.get_field(role_tag, "negotiation_log")
    store.set_field(
        role_tag,
        "negotiation_log",
        [
            *log_entries,
            NegotiationLogEntry(role_tag=role_tag, event=event, round=round_num, ts=now_iso()),
        ],
        notify_channel="negotiation_log",
    )


def _escalate(store: StateStore, role_tag: str, protocol: InteractionProtocol) -> None:
    records = store.get_field(role_tag, "escalation_records")
    store.set_field(
        role_tag,
        "escalation_records",
        [
            *records,
            EscalationRecord(
                trigger_edge=protocol.edge,
                reason=protocol.escalation_trigger or "max_rounds_exhausted",
                target_role="human_manager",
                status="open",
            ),
        ],
        notify_channel="escalation_records",
    )
    _append_log(store, role_tag, "escalation_triggered", None)


async def run_forecast_supply_round(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    agent_index: int,
    pool_id: str,
    initial_proposed: float,
) -> float | None:
    """forecast_agents[agent_index] ↔ capacity_pools(pool_id) 라운드 협상을
    수렴 또는 max_rounds 소진까지 진행한다.

    반환값: 수렴 시 최종 합의 수량, max_rounds 소진(escalation) 시 None.
    """
    protocol = _find_protocol(store, forecast_role_tag, EDGE)

    current_proposed = initial_proposed
    round_num = 1
    final_value: float | None = None

    while round_num <= protocol.max_rounds:
        pools = store.get_field(supply_role_tag, "capacity_pools")
        pool = next(p for p in pools if p.pool_id == pool_id)
        response = supply_coordination_respond(pool.remaining_capacity, current_proposed)

        agent = store.get_field(forecast_role_tag, f"forecast_agents[{agent_index}]")
        updated_agent = agent.model_copy(
            update={
                "current_round": round_num,
                "round_history": [
                    *agent.round_history,
                    ForecastRound(
                        round=round_num,
                        proposed={"quantity": current_proposed},
                        response=response,
                    ),
                ],
            }
        )
        store.set_field(
            forecast_role_tag,
            f"forecast_agents[{agent_index}]",
            updated_agent,
            notify_channel="forecast_agents",
        )
        _append_log(store, forecast_role_tag, f"round_{round_num}_{response['status']}", round_num)

        if response["status"] == "accepted":
            final_value = current_proposed
            break

        judgment = await forecast_next_proposal(current_proposed, response["quantity"])
        next_proposed = judgment.judgment["next_proposed"]
        change_rate = abs(current_proposed - next_proposed) / current_proposed

        if change_rate < CONVERGENCE_THRESHOLD:
            final_value = next_proposed
            break

        current_proposed = next_proposed
        round_num += 1

    if final_value is not None:
        _append_log(store, forecast_role_tag, "negotiation_converged", round_num)
    else:
        _escalate(store, forecast_role_tag, protocol)

    return final_value


async def run_forecast_select_and_round(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    agent_index: int,
    pool_id: str,
    candidates: list[ForecastCandidate],
) -> float | None:
    """analysis 스텁의 candidate 중 하나를 forecast가 선택한 뒤, 그 값을 최초
    제안으로 supply_coordination과 라운드 협상을 시작하는 M1 진입점."""
    selection = select_forecast_candidate(candidates)

    agent = store.get_field(forecast_role_tag, f"forecast_agents[{agent_index}]")
    updated_agent = agent.model_copy(
        update={"selected": selection.judgment["selected"], "selection_basis": "rule"}
    )
    store.set_field(
        forecast_role_tag,
        f"forecast_agents[{agent_index}]",
        updated_agent,
        notify_channel="forecast_agents",
    )
    _append_log(
        store, forecast_role_tag, f"candidate_selected_{selection.judgment['selected']}", None
    )

    return await run_forecast_supply_round(
        store,
        forecast_role_tag,
        supply_role_tag,
        agent_index,
        pool_id,
        initial_proposed=selection.judgment["value"],
    )
