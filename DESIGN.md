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
- **M1 (forecast↔supply_coordination 라운드 협상, MILESTONES.md M1)**:
  `src/sop/judgment.py`에 판단 스텁 공통 반환 스키마 `StructuredJudgment`
  ({judgment, reasoning}) 정의(공통 규칙 2 — M7에서 LLM 구조화 출력으로 내부만
  교체될 지점 전부가 이 스키마를 공유). `src/sop/analysis_stub.py`는 analysis
  agent 실물(M3) 전까지 하드코딩된 a/b/c candidate를 반환하는 스텁.
  `src/sop/forecast_candidate_selection.py`의 `select_forecast_candidate`는 그
  candidate 중 confidence가 가장 높은 것을 규칙 기반으로 선택해
  `StructuredJudgment`로 반환. `src/sop/forecast_supply_round.py`에는:
  `supply_coordination_respond`(remaining_capacity와 proposed만 보고
  accepted/counter를 정하는 순수 함수 — 회사 1개뿐이라 배분 대안이 없어 이번
  마일스톤은 agent 판단이 아님), `forecast_next_proposal`(counter를 받으면
  격차의 50%만 좁혀 재제안, `StructuredJudgment` 반환), `run_forecast_supply_round`
  (라운드 루프 — `forecast_agents[i].round_history`에 매 라운드 누적,
  `interaction_protocol`의 `max_rounds`를 안전장치로 사용, 수렴조건은 직전 대비
  proposed 변화율 < 10%(GRAPH_FLOW.md 원래 정의의 축소판 — 상세는 아래 "미구현"
  참고), max_rounds 소진 시 `escalation_records`에 기록), `run_forecast_negotiation`
  (candidate 선택 → `forecast_agents[i].selected`/`selection_basis` 기록 →
  라운드 루프까지 잇는 진입점). pytest 5건(candidate 선택 규칙, 수렴/max_rounds
  소진 escalation/즉시 accepted, 선택→협상 통합 흐름). `capacity_pools.remaining_capacity`를
  최초 제안보다 작게 둔 상태에서 `run_forecast_supply_round`를 돌리면, 매
  라운드 `forecast_agents[i].round_history`에 기록되는 `proposed` 값이 실제로
  달라지며 좁혀짐을 확인(예: remaining=70일 때 100→85→77.5) — "서로의 판단에
  실제로 영향을 주는 다회 협상"이라는 프로젝트 핵심 목표(CLAUDE.md)가 최소
  골격 수준에서 성립함을 pytest로 확인.

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
- **capacity_pools 동시 읽기에 Lock을 걸지**: M1(`forecast_supply_round.py`)의
  `supply_coordination_respond`는 `capacity_pools`를 Lock 없이 읽기만
  한다 — 회사가 1개뿐이라 두 태스크가 같은 remaining_capacity를 동시에 보고
  둘 다 accepted로 착각할 여지가 없기 때문. 회사가 여러 개로 늘어나는
  마일스톤에서는 "여러 forecast가 동시에 같은 pool의 remaining_capacity를
  읽고 판단"하는 상황이 생기므로, 그때 read 구간까지 Lock으로 감쌀지
  (판단 자체가 아니라 판단에 쓰는 스냅샷의 일관성 문제) 재검토 필요.
  배경은 JOURNAL.md 2026-09-16 참고.
- **수렴한 협상의 최종 합의 수량을 State 어디에 남길지**: M1
  `run_forecast_supply_round`는 수렴 시 최종 수량(예: 77.5)을 함수
  반환값으로만 넘기고 State 어디에도 쓰지 않는다 — `round_history`의
  마지막 항목은 그 수량이 아니라 직전 counter 응답(예: 70)을 담고 있어,
  반환값을 안 받으면 그 수량 자체가 유실된다. 지금은 이 값을 받는
  소비자(예: allocation_candidates 생성)가 아직 없어 저장 위치를 정하지
  않았다 — `forecast_agents[i].selected`에 넣을지, `round_history`에 마지막
  라운드로 하나 더 append할지는 그 소비자가 생기는 마일스톤에서 정한다.
- **수렴값이 remaining_capacity를 초과할 수 있음**: M1 수렴조건(직전 대비
  proposed 변화율 < 10%)은 forecast 자신의 제안이 더 이상 크게 안 바뀌는지만
  보고, supply_coordination이 그 값을 실제로 accepted했는지는 보지 않는다.
  그 결과 수렴된 최종값이 remaining_capacity를 초과할 수 있다(예:
  remaining=70인데 최종 수렴값 77.5 — `test_negotiation_converges_within_max_rounds`에서
  라운드 2 응답도 여전히 counter(70)인 상태로 77.5가 수렴값이 됨). 위
  "최종 합의 수량을 State 어디에 남길지"와는 별개 문제 — 저장 위치를
  정해도 그 값 자체가 remaining_capacity 초과일 수 있다는 게 핵심이다.
  GRAPH_FLOW.md 원래 종료조건(변화폭+forecast_reliability 게이트, M6)이
  붙어도 "제안 쪽 변화만 보고 상대 수락 여부는 안 본다"는 이 문제 자체는
  안 풀릴 수 있음. 이 수렴값을 읽는 소비자(예: allocation_candidates 생성)가
  생기기 전에, capacity 상한을 다시 확인하는 절차를 수렴 로직에 넣을지
  결정해야 한다.
- **candidate 선택의 판단3계층 조건 분기 미구현**: STATE_SCHEMA.md는 forecast의
  candidate 선택을 "신뢰구간이 좁으면 규칙(①), 비용-리스크 트레이드오프가
  얽히면 agent판단(②), 통계와 비즈니스 판단이 충돌하면 사람(③)"으로 나누지만,
  `select_forecast_candidate`(`forecast_candidate_selection.py`)는 이 조건
  판정 없이 confidence 최댓값을 항상 규칙(①)으로 채택한다. "신뢰구간이
  좁다"를 무엇으로 판정할지(예: candidate 간 confidence 격차, 표준편차 등)
  자체가 아직 미정이라 조건 분기를 뒤로 미뤘다 — ②/③ 분기 조건과 함께
  다음 마일스톤에서 정한다.

## 미구현 / todo 필드

State/설계에는 자리가 있지만 아직 실제 로직이 안 붙은 부분.

- **forecast_reliability 신뢰도 게이트**: GRAPH_FLOW.md 엣지 표의
  forecast↔supply_coordination 수렴조건은 "변화폭 임계치 이하 **+**
  forecast_reliability 신뢰도 게이트 통과"이지만, walk-forward validation과
  SQLite 영속화(STATE_SCHEMA.md "forecast_reliability 산출과 저장")가 아직
  없어 M1(`src/sop/forecast_supply_round.py`)은 변화율 조건만으로 수렴을 판정한다.
  영속화가 붙으면 `run_forecast_supply_round`의 수렴 분기에 게이트를
  추가해야 함(M6, MILESTONES.md 참고).

## 확장 지점

지금은 안 만들지만 구조적으로 열어둔 부분(예: interaction_protocol의
promoted_from_trace 경로, 공급망계획agent 간 직접 협상 등).

(TBD)

## 실무 전환 시 고려사항

Step1의 "하지 않은 것" 목록과 같은 성격 — 정직한 스코프 명시용. mock/샘플링 데이터,
실제 배포, 실시간 다중 사용자 등 실무 전환 시 별도로 다뤄야 할 것들.

(TBD)
