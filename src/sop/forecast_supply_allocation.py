"""forecast agent → supply_coordination agent 단방향 최적화 배분 (M1).

GRAPH_FLOW.md "상호작용 세 가지 유형"의 "최적화"에 해당하는 정방향 edge —
forecast가 선택한 candidate 값을 그대로 담아 supply_coordination이
`allocation_candidate` 하나를 생성한다. 라운드도, capacity 비교도, 응답
대기도 없다(우선순위 점수 산출은 회사가 1개뿐이라 경쟁 자체가 없어
이 마일스톤에서는 값을 그대로 반영하는 것으로 근사 — 실제 다회사 경쟁
로직은 M4).

역방향(supply_coordination→forecast, plan agent의 infeasible이 트리거하는
핸드오프)은 plan agent 자체가 아직 없어(M5) 이 모듈에서 다루지 않는다.

`run_forecast_select_and_allocate`가 candidate 선택
(`forecast_candidate_selection.py`) → 이 배분 생성까지 잇는 상위 진입점이다.
"""

from .access import StateStore
from .forecast_candidate_selection import select_forecast_candidate
from .ids import new_id, now_iso
from .state import AllocationCandidate, ForecastCandidate, NegotiationLogEntry


def allocate_forecast_candidate(forecast_agent_id: str, value: float) -> AllocationCandidate:
    """forecast가 선택한 candidate 값을 그대로 담아 allocation_candidate 1개를 생성.

    우선순위 점수 산출은 회사가 1개뿐이라 경쟁 자체가 없어(M4에서 실제
    다회사 경쟁 로직 구현), 이번 마일스톤에서는 supply_coordination의
    agent 판단이 아니라 순수 함수로 근사한다.
    """
    return AllocationCandidate(
        plan_id=new_id("PLAN"),
        allocation={forecast_agent_id: value},
    )


def _append_log(store: StateStore, role_tag: str, event: str) -> None:
    log_entries = store.get_field(role_tag, "negotiation_log")
    store.set_field(
        role_tag,
        "negotiation_log",
        [*log_entries, NegotiationLogEntry(role_tag=role_tag, event=event, ts=now_iso())],
        notify_channel="negotiation_log",
    )


async def run_forecast_select_and_allocate(
    store: StateStore,
    forecast_role_tag: str,
    supply_role_tag: str,
    agent_index: int,
    candidates: list[ForecastCandidate],
) -> AllocationCandidate:
    """analysis 스텁의 candidate 중 하나를 forecast가 선택한 뒤, 그 값을 그대로
    담아 supply_coordination이 `allocation_candidate`를 생성하는 M1 진입점
    (단방향 최적화, 라운드 없음)."""
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
    _append_log(store, forecast_role_tag, f"candidate_selected_{selection.judgment['selected']}")

    candidate = allocate_forecast_candidate(agent.agent_id, selection.judgment["value"])
    store.set_field(
        supply_role_tag,
        "allocation_candidates",
        [*store.get_field(supply_role_tag, "allocation_candidates"), candidate],
        notify_channel="allocation_candidates",
    )
    _append_log(store, supply_role_tag, f"allocation_candidate_generated_{candidate.plan_id}")

    return candidate
