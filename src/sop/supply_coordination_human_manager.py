"""`supply_coordination->human_manager` 엣지의 `interaction_protocol` 항목 둘(STATE_SCHEMA.md 5번 절).

- `commitment_gap`: 배분이 최소 구매 약정의 이번 주기 몫과 기준 이상 차이 나면 알리고 진행한다(`notice`). 구속력이 없으면 우리가 손실을
  떠안으므로 더 작은 차이에도 알린다.
- `commitments_exceed_capacity`: 미납분과 계약 하한을 합쳐 그 라인의 공급 가능량을 넘으면 어느 계약을 줄일지 사람이 정한다
  (`intervention`). 모드를 트레이싱·처리 결과를 보고 바꿀 수 있게 항목을 둔다.

조회 키는 `(edge, escalation_trigger)`다(`interaction_protocol.find_protocol_entry`).
"""

from .ids import now_iso
from .state import InteractionProtocol, NoticeThreshold

ESCALATION_EDGE = "supply_coordination->human_manager"


def commitment_gap_protocol_entry() -> InteractionProtocol:
    return InteractionProtocol(
        edge=ESCALATION_EDGE,
        scope=["supply_coordination", "human_manager"],
        escalation_trigger="commitment_gap",
        escalation_target="human_manager",
        escalation_kind="rule",
        escalation_mode="notice",
        notice_threshold=NoticeThreshold(binding=0.2, non_binding=0.05),
        source="initial_design",
        last_updated=now_iso(),
    )


def commitments_exceed_capacity_protocol_entry() -> InteractionProtocol:
    return InteractionProtocol(
        edge=ESCALATION_EDGE,
        scope=["supply_coordination", "human_manager"],
        escalation_trigger="commitments_exceed_capacity",
        escalation_target="human_manager",
        escalation_kind="human",
        escalation_mode="intervention",
        source="initial_design",
        last_updated=now_iso(),
    )
