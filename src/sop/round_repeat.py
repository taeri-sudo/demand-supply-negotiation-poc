"""라운드 누적형 엣지의 같은 의심되는 원인의 연속 횟수: 같은 이유가 `repeat_escalation_threshold`번 연속 반복되면
`max_rounds` 소진을 기다리지 않고 상위로 확장할지 판단하는 헬퍼 (GRAPH_FLOW.md "검증agent" `failed`).

이 판단은 `failed`를 받은 작성agent의 몫이고 검증agent는 관여하지 않는다. 연속 횟수는 저장하지 않고 그 엣지의
`exchanges`를 최근 것부터 훑어 그때그때 계산한다. 센 횟수는 엣지와 이유 단위로만 유효하다 — 다른 엣지로 넘어가면 그
agent의 기록에서 새로 센다(누적 이월 없음). 라운드 누적형 엣지에서만 쓰며, 라운드가 없는 엣지
(forecast↔supply_coordination 등)는 쓰지 않는다. 지금 라운드 누적형 엣지는 M5에서 생기므로 테스트는 합성한
`exchanges`로 확인한다.
"""

from .interaction_protocol import find_edge_entry
from .state import Exchange, InteractionProtocol, suspected_cause_reason


def exchange_reason(exchange: Exchange) -> str | None:
    """교환 하나의 이유: 공급망조율의 `routing_reason`, 없으면 `failed` 판정의 의심되는 원인들. 둘 다 없으면 None."""
    if exchange.routing_reason:
        return exchange.routing_reason
    validation = exchange.validation
    if validation is not None and validation.status == "failed":
        return ",".join(sorted({suspected_cause_reason(c) for c in validation.suspected_causes}))
    return None


def consecutive_repeats(exchanges: list[Exchange]) -> tuple[str | None, int]:
    """가장 최근 교환의 이유와, 그 이유가 최근부터 연속된 횟수. 최근 교환에 이유가 없으면 (None, 0)."""
    if not exchanges:
        return None, 0
    latest = exchange_reason(exchanges[-1])
    if latest is None:
        return None, 0
    count = 0
    for exchange in reversed(exchanges):
        if exchange_reason(exchange) != latest:
            break
        count += 1
    return latest, count


def repeat_escalation_reason(
    entries: list[InteractionProtocol], writer_role_tag: str, counterpart_role_tag: str, exchanges: list[Exchange]
) -> str | None:
    """`interaction_protocol`의 두 역할 사이 엣지에서 같은 이유가 `repeat_escalation_threshold`번 연속 반복됐으면 그
    이유를 반환한다(상위로 확장할 때). 엣지 항목이 없거나 임계값이 없거나 반복이 모자라면 None이다.

    `exchanges`는 후보안의 교환 목록이고, 그중 `counterpart_role_tag`와의 교환만 그 엣지의 기록이다.
    """
    entry = find_edge_entry(entries, writer_role_tag, counterpart_role_tag)
    if entry is None or entry.repeat_escalation_threshold is None:
        return None
    reason, count = consecutive_repeats([e for e in exchanges if e.role_tag == counterpart_role_tag])
    return reason if reason is not None and count >= entry.repeat_escalation_threshold else None
