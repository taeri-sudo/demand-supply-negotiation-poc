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
- **M1 (MILESTONES.md M1 — 2026-09-20 설계 재정의, 코드는 아직 구설계
  그대로라 재작업 필요)**: 최초 구현은 forecast↔supply_coordination을
  라운드 협상(격차 50% 좁히기, 변화율 수렴조건)으로 다뤘으나, 이후 설계
  검토로 이 메커니즘 자체가 무효화됐다(JOURNAL.md 2026-09-16/2026-09-20
  참고). `src/sop/forecast_supply_round.py` 등 M1 코드는 여전히 이 옛
  설계를 그대로 구현하고 있어 재작업이 필요하다.

  현재 설계(STATE_SCHEMA.md/AGENT_NODE_LIST.md/GRAPH_FLOW.md에 이미 반영):
  - analysis agent와 forecast agent를 하나(`forecast_agents[]`, role_tag:
    `forecast`)로 통합 — 데이터 수집(함수) → 데이터 소스 판단 → 모델
    선택 → 시나리오 계산(함수) → 후보 선택, 전부 한 agent 내부 단계.
  - 되돌림(핸드오프)은 `suspected_cause` 두 값만: "데이터 소스 문제"
    (데이터 수집부터 재실행)/"모델 선택 문제"(모델 선택부터 재실행).
  - `forecast → supply_coordination`(정방향, 평소): 단방향 전달(최적화)
    — 후보 선택값을 supply_coordination이 우선순위 점수 산출 후
    `allocation_candidate`로 생성, 라운드 없음.
  - `supply_coordination → forecast`(역방향, 예외): 공급망계획agent의
    infeasible 신호로만 열리는 핸드오프(재실행 지시) — 라운드 협상이
    아니라, 위 되돌림 메커니즘을 그대로 재사용.
  - 검증agent는 판정만 하고 라우팅은 안 함(관찰형) — `passed`의 push
    여부는 받는 agent 성격에 따라 갈림(워커풀 성격 `procurement_plan`
    등은 스스로 pull, 조율 성격 `supply_coordination`은 push), `flagged`는
    항상 작성agent에게 push.

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
