# DESIGN.md

demand-supply-negotiation-poc의 **현재 구현 상태**를 담는 문서. State/agent/그래프
흐름의 최신 설계는 STATE_SCHEMA.md/AGENT_NODE_LIST.md/GRAPH_FLOW.md를 직접 참고 —
값이 바뀌면 그 파일들을 직접 고치고, 여기서는 중복 서술하지 않는다. "왜 그렇게
됐는지"의 논거는 JOURNAL.md 참고.

## 진행 상황

- **M0 (State 스켈레톤 + 접근통제 wrapper)**: `src/sop/state.py`에 State
  최상위 필드 전체(필드 수는 STATE_SCHEMA.md 참고 — M1에서
  analysis_agents가 forecast_agents로 통합되며 바뀜)를 pydantic
  `BaseModel`로 정의. `src/sop/access.py`에
  `StateStore.get_field`/`set_field`(role_permissions 검사, set 시 큐 push
  동시 수행) 구현. `src/sop/capacity.py`에 `capacity_pools` 증감용
  `asyncio.Lock` 보호 헬퍼(`adjust_capacity_pool`) 구현. agent 로직은 아직
  없음(pytest 8건 — 권한 거부, set_field-큐 push 짝, 중첩 field_path
  읽기/쓰기, 와일드카드 권한, Lock 유무에 따른 동시성 경쟁 재현/해결).
- **M1 (MILESTONES.md M1 — 2026-09-21 코드를 2026-09-20 설계에 맞춰
  리팩터링)**: 최초 구현은 forecast↔supply_coordination을 라운드 협상
  (격차 50% 좁히기, 변화율 수렴조건)으로 다뤘으나, 설계 검토로 이
  메커니즘 자체가 무효화됐다(JOURNAL.md 2026-09-16/2026-09-20 참고).
  `src/sop/forecast_supply_round.py`(라운드 루프, `supply_coordination_respond`,
  `forecast_next_proposal`, 관련 상수)를 삭제하고 `src/sop/forecast_supply_allocation.py`로
  교체 — `allocate_forecast_candidate`(candidate 값을 그대로 담아
  `allocation_candidate` 1개 생성, 우선순위 점수 산출은 회사 1개뿐이라
  경쟁이 없어 이 값 그대로 근사 — 실제 경쟁 로직은 M4), `run_forecast_select_and_allocate`
  (candidate 선택 → 위 생성까지 잇는 상위 진입점, 이전 `run_forecast_select_and_round`의
  후신). capacity_pools/interaction_protocol을 더 이상 참조하지 않음
  (테스트에서 해당 권한을 아예 안 줘서 직접 확인). pytest 3건(값이 그대로
  전달되는지, `allocation_candidate` 1개 생성, negotiation_log 순서).
  파일명은 `forecast_supply_round.py` → `forecast_supply_allocation.py`로
  변경(CLAUDE.md 엣지 기반 파일명 규칙 — 더 이상 라운드가 없는데 "round"가
  이름에 남으면 실제 동작과 안 맞음).

  역방향(`supply_coordination → forecast`, plan agent의 infeasible이
  트리거하는 핸드오프)은 plan agent 자체가 아직 없어(M5) 이번
  리팩터링에서 구현하지 않음 — 트리거가 없다는 것만 확인.

  **후속 정리(같은 날 2026-09-21, 두 번째 패스)**: 위 리팩터링 중 발견한
  스키마 불일치를 마저 정리했다 — `AnalysisAgentRecord`/`analysis_agents`
  삭제, 그 필드(`data_source_basis`/`model_selection`/`candidates`)를
  `ForecastAgentRecord`로 흡수(전부 선택 필드 — 실제 데이터 소스 판단·
  모델 선택 로직은 아직 없음, M3 공백은 그대로 남음). `ForecastAgentRecord`의
  `current_round`/`round_history`, `ForecastRound` 클래스 삭제(라운드
  협상 전제, 어느 방향도 더 이상 라운드가 없음). `InteractionProtocol`의
  `max_rounds`/`repeat_escalation_threshold`는 삭제 대신 선택 필드로
  전환(`int | None = None`) — forecast<->supply_coordination에는 안
  쓰이지만 supply_coordination↔procurement_plan 등(M5)에는 여전히
  필요하기 때문. interaction_protocol을 소비하는 코드는 여전히 없음 —
  검증agent(M2)가 아직 구현 안 됐을 뿐이라 의도된 상태, M2 착수 시 처리.
  기존 pytest 12건 그대로 통과(이 스키마 변경을 직접 건드리는 테스트가
  없었음).

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
  성질이라 그대로 채택. 다만 실무 전환 시 주기 시작 시점에 forecast
  태스크가 한꺼번에 몰릴 수 있어(N개 회사가 같은 시각에 트리거), 현재
  validation agent에만 적용한 워커풀 패턴을 forecast에도 적용할
  여지를 열어둔다.

## 아직 결정 안 된 것 / 다음에 확인할 것

(TBD — 예: 재무(9.0) 포함 여부 등 STEP2_SUMMARY.md에 이미 미정으로
남아있는 것들이 여기로 옮겨올 수 있음)

- **forecast_agents의 인스턴스 단위 재설계 가능성 — 회사
  단위에서 (회사, item) 단위로**: 지금은 "회사 1개 = 인스턴스 1개"
  (MILESTONES.md M1의 "회사 1개"/M4의 "N개 회사 확장" 등이 전부 이 전제)
  인데, 한 회사가 여러 item(예: 라면과 과자)을 동시에 주문하는 경우를
  표현할 수 없다는 구조적 한계가 발견됨. 아직 구현/문서 반영 전 —
  확정되면 MILESTONES.md의 인스턴스 단위를 전제한 서술을 전부 재검토해야
  함(재검토 트리거로 남겨둠). 별도 세션에서 STATE_SCHEMA.md부터 재설계
  예정. 상세 논거는 JOURNAL.md 2026-09-19 참고.
- **forecast_agents의 data_source_basis/model_selection 실물 로직이
  아직 없음(M3 공백)**: 2026-09-21 리팩터링으로 `state.py`의 스키마
  자체는 STATE_SCHEMA.md 통합 스키마와 맞췄지만(`ForecastAgentRecord`가
  `data_source_basis`/`model_selection`/`candidates`를 흡수, 라운드 전제
  필드 삭제), 이 필드를 실제로 채우는 데이터 소스 판단·모델 선택 판단
  로직 자체는 여전히 없다 — analysis agent 실물 구현을 맡았던 M3가
  성립하지 않게 되며 생긴 공백(MILESTONES.md M3 참고)으로, 어느
  마일스톤이 이 실물 구현을 맡을지 아직 안 정했다.
- **procurement_plan이 여러 forecast 요청을 묶어 처리하는 게 나은지**: 여러
  forecast agent의 요청을 procurement_plan이 묶어서 처리(대량구매 단가 등)
  하는 게 나은지는 지금 넣지 않는다 — AGENT_NODE_LIST.md 설계(안건별 개별
  처리)와 다른 새 판단 로직이 필요하고 비용도 드는 일이라, GRAPH_FLOW.md의
  `promoted_from_trace` 승격 경로(공급망계획agent 간 직접 상호작용 미정과
  같은 방식)로 미룬다. `negotiation_log`에서 같은 시기 여러 요청이 자주
  겹치는 패턴이 실제로 드러나면 그때 추가할 후보로만 기록해둔다.
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
- **candidate 선택의 판단3계층 조건 분기 미구현**: STATE_SCHEMA.md는
  forecast agent의 (통합된 내부 단계 중) 후보 선택을 "신뢰구간이 좁으면
  규칙(①), 비용-리스크 트레이드오프가 얽히면 agent판단(②), 통계와
  비즈니스 판단이 충돌하면 사람(③)"으로 나누지만, `select_forecast_candidate`
  (`forecast_candidate_selection.py`)는 이 조건 판정 없이 confidence
  최댓값을 항상 규칙(①)으로 채택한다. "신뢰구간이 좁다"를 무엇으로
  판정할지(예: candidate 간 confidence 격차, 표준편차 등) 자체가 아직
  미정이라 조건 분기를 뒤로 미뤘다 — ②/③ 분기 조건과 함께 다음
  마일스톤에서 정한다.

## 미구현 / todo 필드

State/설계에는 자리가 있지만 아직 실제 로직이 안 붙은 부분.

- **forecast_reliability 신뢰도 게이트**: 원래 forecast↔supply_coordination의
  수렴조건("변화폭 임계치 이하 **+** forecast_reliability 게이트 통과")에
  붙는 걸로 전제했으나, 그 엣지 성격이 바뀌면서(forecast→supply_coordination은
  라운드 없는 단방향 최적화, supply_coordination→forecast는 핸드오프 —
  GRAPH_FLOW.md 참고) 이 게이트가 정확히 어디에 붙어야 하는지 재정의가
  필요하다. forecast_reliability 자체는 우선순위 구조 tier 2(STATE_SCHEMA.md
  "우선순위 구조" 참고 — supply_coordination의 배분 우선순위 산출에 쓰임)로는
  여전히 유효해 보이지만, walk-forward validation과 SQLite 영속화
  (STATE_SCHEMA.md "forecast_reliability 산출과 저장")가 아직 없어 어느
  쪽이든 M6 전까지는 구현되지 않는다(MILESTONES.md M6 참고).

## 확장 지점

지금은 안 만들지만 구조적으로 열어둔 부분(예: interaction_protocol의
promoted_from_trace 경로, 공급망계획agent 간 직접 협상 등).

(TBD)

## 실무 전환 시 고려사항

Step1의 "하지 않은 것" 목록과 같은 성격 — 정직한 스코프 명시용. mock/샘플링 데이터,
실제 배포, 실시간 다중 사용자 등 실무 전환 시 별도로 다뤄야 할 것들.

(TBD)
