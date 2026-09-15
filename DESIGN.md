# DESIGN.md

demand-supply-negotiation-poc의 **현재 구현 상태**를 담는 문서. State/agent/그래프
흐름의 최신 설계는 STATE_SCHEMA.md/AGENT_NODE_LIST.md/GRAPH_FLOW.md를 직접 참고 —
값이 바뀌면 그 파일들을 직접 고치고, 여기서는 중복 서술하지 않는다. "왜 그렇게
됐는지"의 논거는 JOURNAL.md 참고.

## 진행 상황

- **M0 (State 스켈레톤 + 접근통제 wrapper)**: `src/sop/state.py`에 State
  8개 최상위 필드를 pydantic `BaseModel`로 정의. `src/sop/access.py`에
  `StateStore.get_field`/`set_field`(role_permissions 검사, set 시 큐 push
  동시 수행) 구현. `src/sop/capacity.py`에 `capacity_pools` 증감용
  `asyncio.Lock` 보호 헬퍼(`adjust_capacity_pool`) 구현. agent 로직은 아직
  없음(pytest 8건 — 권한 거부, set_field-큐 push 짝, 중첩 field_path
  읽기/쓰기, 와일드카드 권한, Lock 유무에 따른 동시성 경쟁 재현/해결).

## 검토 후 현재 구조 유지로 확정

사용자와 실제로 논의한 뒤 원래 구조 그대로 가기로 확정한 결정들만 담는다.
구현 중 스스로 내린 설계 판단(대안을 비교했든 아니든)은 여기 넣지 않고
"진행 상황"에 구현 설명으로만 남긴다. 상세 논거는 JOURNAL.md 참고.

- **실행 리듬 — 실시간 연속 스트림이 아니라 계획 주기(월간 등) 기반**:
  초기에는 이벤트가 생기면 언제든 즉시 사이클이 도는 실시간 자율 반응형을
  지향했으나, PCF/S&OP가 원래 월간 등 주기적 계획 프로세스라는 도메인
  근거를 따라가며 재확인됨. 사이클의 **시작**은 계획 주기에 묶되, 사이클이
  도는 동안의 값 변경 전파(값 쓰기 시 즉시 큐 신호)는 그대로 이벤트
  반응형을 유지 — 이건 구현으로 바꿀 수 있는 선택이 아니라 도메인 자체의
  성질이라 그대로 채택. 다만 실무 전환 시 주기 시작 시점에 forecast/analysis
  태스크가 한꺼번에 몰릴 수 있어(N개 회사가 같은 시각에 트리거), 현재
  validation agent에만 적용한 워커풀 패턴을 forecast/analysis에도 적용할
  여지를 열어둔다.

## 아직 결정 안 된 것 / 다음에 확인할 것

(TBD — 예: 공급망계획agent 간 직접 상호작용 여부, 재무(9.0) 포함 여부 등
STEP2_SUMMARY.md에 이미 미정으로 남아있는 것들이 여기로 옮겨올 수 있음)

- **role_permissions에 `w`만 있고 대응하는 `r`이 없는 조합을 막을지**:
  지금 코드(`access.py`)는 이 조합을 허용한다. `negotiation_log`/
  `escalation_records`처럼 "기록용 스트림"에 이벤트를 append만 하고, 판단은
  그 기록을 다시 읽지 않고 현재값 필드(`round_history`/`exchanges` 등)로만
  하는 역할이라면 w-only가 자연스러울 수 있어(예: forecast가 자기 라운드
  이벤트를 negotiation_log에 쓰기만 하고 판단엔 round_history를 씀), 항상
  실수(r을 빠뜨린 오탈자)라고 단정할 근거가 아직 없음. 지금은 실제
  agent 코드가 없어 이런 패턴이 나타날지 확인 불가 — M1 이후 실제
  agent별 role_permissions가 채워지면 w-only 조합이 실제로 나타나는지,
  나타난다면 의도된 것인지 보고 그때 pydantic `model_validator`로 막을지
  재판단한다.
- **field_path 조건부 선택을 caller-side 헬퍼로 뽑아낼지**: 지금은
  agent마다 인덱스 탐색을 직접 하게 돼 있음. M1~M5에서 이 탐색이
  반복되는 정도를 보고 헬퍼 추가 여부 재검토(wrapper 자체는 안 바꿈).
  배경은 JOURNAL.md 2026-09-15 참고.

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
