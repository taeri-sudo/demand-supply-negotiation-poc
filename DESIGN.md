# DESIGN.md

demand-supply-negotiation-poc의 **현재 구현 상태**를 담는 문서. State/agent/그래프
흐름의 최신 설계는 STATE_SCHEMA.md/AGENT_NODE_LIST.md/GRAPH_FLOW.md를 직접 참고 —
값이 바뀌면 그 파일들을 직접 고치고, 여기서는 중복 서술하지 않는다. "왜 그렇게
됐는지"의 논거는 JOURNAL.md 참고.

## 진행 상황

(TBD — 구현 진행하며 채움)

## 검토 후 현재 구조 유지로 확정

바꿀지 고민했지만 결국 원래 구조 그대로 가기로 한 결정들. 나중에 같은 고민을
반복하지 않기 위해 남긴다. 상세 논거는 JOURNAL.md 참고.

(TBD)

## 아직 결정 안 된 것 / 다음에 확인할 것

(TBD — 예: 공급망계획agent 간 직접 상호작용 여부, 재무(9.0) 포함 여부 등
STEP2_SUMMARY.md에 이미 미정으로 남아있는 것들이 여기로 옮겨올 수 있음)

## 미구현 / todo 필드

State/설계에는 자리가 있지만 아직 실제 로직이 안 붙은 부분.

(TBD)

## 확장 지점

지금은 안 만들지만 구조적으로 열어둔 부분(예: interaction_protocol의
promoted_from_trace 경로, 공급망계획agent 간 직접 협상 등).

(TBD)

## 실무 전환 시 고려사항

Step1의 "하지 않은 것" 목록과 같은 성격 — 정직한 스코프 명시용. mock/샘플링 데이터,
실제 배포, 실시간 다중 사용자 등 실무 전환 시 별도로 다뤄야 할 것들.

(TBD)
