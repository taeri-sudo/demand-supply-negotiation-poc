"""`interaction_protocol[]` 조회 헬퍼. 엣지의 기준값은 코드에 두지 않고 이 표에서 읽는다(MILESTONES.md 공통 규칙 1).

항목의 조회 키는 `(edge, escalation_trigger)`다(같은 엣지에 트리거가 여러 개일 수 있다). 두 역할 사이 엣지는 `scope`로도 찾는다.
"""

from .state import InteractionProtocol


def find_edge_entry(entries: list[InteractionProtocol], role_a: str, role_b: str) -> InteractionProtocol | None:
    """두 역할 사이의 edge 항목. `scope`가 두 역할로 정확히 이루어진 항목을 방향과 상관없이 찾는다.

    같은 쌍의 알림 엣지(`->human_manager`)는 scope에 human_manager가 들어 있어 사람이 아닌 역할 쌍과 겹치지 않는다.
    """
    pair = {role_a, role_b}
    for entry in entries:
        if set(entry.scope) == pair:
            return entry
    return None


def find_protocol_entry(
    entries: list[InteractionProtocol], edge: str, escalation_trigger: str
) -> InteractionProtocol | None:
    """같은 엣지에 트리거가 여러 개일 수 있어 `(edge, escalation_trigger)`로 찾는다."""
    for entry in entries:
        if entry.edge == edge and entry.escalation_trigger == escalation_trigger:
            return entry
    return None
