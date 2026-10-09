"""`interaction_protocol[]` 조회 헬퍼. 검증agent가 edge를 하드코딩하지 않고 이 표를 읽는다(MILESTONES.md 공통 규칙 1)."""

from .state import InteractionProtocol

HUMAN_ROLE = "human_manager"


def find_edge_entry(entries: list[InteractionProtocol], role_a: str, role_b: str) -> InteractionProtocol | None:
    """두 역할 사이의 edge 항목. `scope`가 두 역할로 정확히 이루어진 항목을 방향과 상관없이 찾는다.

    같은 쌍의 알림 엣지(`->human_manager`)는 scope에 human_manager가 들어 있어 사람이 아닌 역할 쌍과 겹치지 않는다.
    """
    pair = {role_a, role_b}
    for entry in entries:
        if set(entry.scope) == pair:
            return entry
    return None


def find_edges_of(entries: list[InteractionProtocol], role_tag: str) -> list[InteractionProtocol]:
    """역할이 다른 agent와 맺은 edge 항목들(사람에게 가는 알림·escalation 엣지는 제외)."""
    return [e for e in entries if role_tag in e.scope and HUMAN_ROLE not in e.scope]
