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

  **후속 정리(2026-09-23, 인스턴스 단위 재설계)**: forecast_agents
  인스턴스 단위를 "회사 1개당 1개"에서 "(company_id, item_id) 조합당
  1개"로 바꿨다(STATE_SCHEMA.md/AGENT_NODE_LIST.md/MILESTONES.md는 이미
  이 단위로 갱신돼 있었음 — 이번 패스에서 코드에 반영). `ForecastAgentRecord`에
  `company_id`(Optional)/`item_id`/`pool_key`(Optional) 필드 추가.
  `forecast_supply_allocation.py`는 forecast_agents 인스턴스를 리스트
  인덱스가 아니라 `_find_forecast_agent_index` 헬퍼로 (company_id,
  item_id) 조회해서 찾도록 바꿨고(`run_forecast_select_and_allocate`의
  `agent_index: int` 파라미터를 `company_id`/`item_id`로 교체),
  `analysis_stub.py`의 `get_stub_candidates`는 `item_id`별로 다른
  candidate 3개를 반환하게 됐다. `CapacityPool.linked_role_tags`(미사용)를
  `linked_pool_key`(단일 str, Optional)로 교체 — STATE_SCHEMA.md의
  "인스턴스 나열이 아니라 자원 공유 단위(태그)로 연결" 방향에 맞춤.
  `tests/test_forecast_supply_allocation.py`에 같은 회사의 서로 다른
  item 인스턴스 2개(라면/과자)를 두고, 한 인스턴스만 갱신했을 때 다른
  인스턴스가 영향받지 않는지 확인하는 테스트 추가. pytest 13건(기존
  12건 갱신 + 신규 1건) 통과. 상세 논거는 JOURNAL.md 2026-09-23 참고.

  **후속 정리(같은 날 2026-09-21, 두 번째 패스)**: 위 리팩터링 중 발견한
  스키마 불일치를 마저 정리했다 — `AnalysisAgentRecord`/`analysis_agents`
  삭제, 그 필드(`data_source_basis`/`model_selection`/`candidates`)를
  `ForecastAgentRecord`로 흡수(전부 선택 필드 — 실제 데이터 소스 판단·
  모델 선택 로직은 아직 없음, M2 공백은 그대로 남음). `ForecastAgentRecord`의
  `current_round`/`round_history`, `ForecastRound` 클래스 삭제(라운드
  협상 전제, 어느 방향도 더 이상 라운드가 없음). `InteractionProtocol`의
  `max_rounds`/`repeat_escalation_threshold`는 삭제 대신 선택 필드로
  전환(`int | None = None`) — forecast<->supply_coordination에는 안
  쓰이지만 supply_coordination↔procurement_plan 등(M5)에는 여전히
  필요하기 때문. interaction_protocol을 소비하는 코드는 여전히 없음 —
  검증agent(M3)가 아직 구현 안 됐을 뿐이라 의도된 상태, M3 착수 시 처리.
  기존 pytest 12건 그대로 통과(이 스키마 변경을 직접 건드리는 테스트가
  없었음).

- **M2 1단계 (스키마 교체, 진행 중인 M2의 일부 — M2 완료 시 M2 전체 요약으로
  합친다)**: `state.py`를 STATE_SCHEMA.md 확정 스키마로 교체했다.
  `forecast_agents`/`ForecastAgentRecord`는 `forecast_records`/`ForecastRecord`로,
  `company_id`는 필수(null 불허). `data_source_basis`/`model_selection`/
  `candidates`/`selected`는 `data_sources`/`excluded_sources`/`cleaning`/
  `forecast_method`/`scenarios`(가정 2단 구조)/`selected_scenario`로 바뀌었고,
  `suspected_cause`는 문자열에서 구조(`SuspectedCause`: type·issue·scenario_id·
  source·use_from)로 바뀌었다. type별로 유효한 issue만 허용하는 검증을
  모델에 넣어(`scenario`는 시나리오 검증 조건 네 값, `data_source`는
  insufficient/contaminated/irrelevant, `forecast_method`는 issue 없음) 잘못된
  조합이 State에 들어가지 않게 했다. 알림용 `interaction_protocol` 항목을
  담도록 `InteractionProtocol`에 `escalation_mode`/`notice_threshold`를, `EscalationRecord`에
  `mode`(intervention/notice)를 추가했다.
  기존 M1 코드는 새 스키마에 맞춰 옮겼다 — `forecast_candidate_selection.py`는
  `forecast_scenario_selection.py`(`select_forecast_scenario`)로 바뀌며 선택 규칙이
  confidence 최댓값에서 AGENT_NODE_LIST.md 6단계 ①의 `cost_estimate` 최솟값으로
  바뀌었다(새 스키마에 confidence 필드가 없음). `analysis_stub.py`는
  `get_stub_scenarios`로 바뀌었고 M2 5단계에서 삭제된다. 판단3계층의 ②/③ 분기와
  최소 구매 약정 적용은 4단계에서 붙는다.
  의존성: 예측기법 라이브러리로 statsforecast(2.1.1)를 도입했다. 이 패키지가
  pandas를 3.0 미만으로 제한해 pandas를 3.0.5에서 2.3.3으로 낮췄고, IPC xlsx를
  읽기 위해 openpyxl을 추가했다(requirements.txt 반영).
  pytest 33건 통과(기존 13건을 새 스키마로 갱신하고 시나리오 선택 1건,
  스키마 제약 18건 추가). 판단 근거는 JOURNAL.md 2026-09-30 참고.

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

- **forecast_records의 데이터 소스 판단·예측기법 선택 등 실물 로직이
  아직 없음(M2 2단계 이후 구현 예정)**: M2 1단계로 `state.py`의 스키마는
  STATE_SCHEMA.md 확정 스키마(`forecast_records`)로 교체됐지만, 이 필드를 실제로
  채우는 시나리오 정의·데이터 소스 판단·예측기법 선택·시나리오별 예측 계산·발생
  가능성 평가 로직은 아직 없고 `analysis_stub.py`의 하드코딩 시나리오가 대신한다.
  M2(MILESTONES.md M2 "forecast agent 실물 판단 로직 구현") 완료 시 이 항목을
  제거한다.
- **시장 데이터(INA-R) 변화율 기준이 미정 — 월별 변화율은 잡음이 커서 문서의
  ±5% 규칙을 그대로 쓰면 모든 상품군에서 위축과 성장이 항상 관측된다**:
  AGENT_NODE_LIST.md forecast agent 1단계는 "시장 데이터에서 -5% 이상 위축과 +5% 이상
  성장이 모두 있었으면 시나리오 2개 추가"라고 정한다. 그런데 INA-R 월별 변화율은
  전월 대비 1차 자기상관이 -0.23에서 -0.61 사이로 되돌아오는 잡음이 커서, 이 규칙을
  월별 변화율에 그대로 쓰면 모든 상품군에서 위축과 성장이 항상 관측된다. 그러면
  MILESTONES.md M2 검증 항목 "시장 데이터에서 관측된 방향 수에 따라 시나리오 개수가
  달라지는지"와 충돌할 수 있다. 기준(월별 ±5% / 전년 동월 대비 / 3개월 이동평균)은
  M2 4단계 착수 전에 사용자가 정한다. 1단계부터 3단계까지는 이 결정과 무관하다.
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
  그 기록을 다시 읽지 않고 현재값 필드(`exchanges` 등)로만
  하는 역할이라면 w-only가 자연스러울 수 있어(예: supply_coordination이 교환
  이벤트를 negotiation_log에 쓰기만 하고 판단엔 exchanges를 씀), 항상
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
  비즈니스 판단이 충돌하면 사람(③)"으로 나누지만, `select_forecast_scenario`
  (`forecast_scenario_selection.py`)는 이 조건 판정 없이 `cost_estimate`
  최솟값을 항상 규칙(①)으로 채택한다. "신뢰구간이 좁다"를 무엇으로
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
  필요하다. forecast_reliability 자체는 우선순위 구조 tier 2(AGENT_NODE_LIST.md
  supply_coordination agent "우선순위 점수 산출" 참고 — 배분 우선순위 산출에
  쓰임)로는 여전히 유효해 보이지만, walk-forward validation과 SQLite 영속화
  (AGENT_NODE_LIST.md supply_coordination agent "예측 신뢰도 입력")가 아직 없어 어느
  쪽이든 M6 전까지는 구현되지 않는다(MILESTONES.md M6 참고).

## 확장 지점

지금은 안 만들지만 구조적으로 열어둔 부분(예: interaction_protocol의
promoted_from_trace 경로, 공급망계획agent 간 직접 협상 등).

(TBD)

## 실무 전환 시 고려사항

Step1의 "하지 않은 것" 목록과 같은 성격 — 정직한 스코프 명시용. mock/샘플링 데이터,
실제 배포, 실시간 다중 사용자 등 실무 전환 시 별도로 다뤄야 할 것들.

**M2 데이터 대체 가정의 한계 (INA-R 상품군 매핑)**: 상품군별 시장 흐름은 다음
두 경우로 나눠 만든다(구현은 M2 2단계). 상품군 하나로 INA-R 3자리 그룹(D코드)
하나를 특정할 수 있으면 그 D코드의 월별 변화율을 가중이나 평균 없이 그대로
쓴다. 특정할 수 없는 상품군(GROCERY I, FROZEN FOODS, DELI)은 D151, D152, D153,
D154 네 코드의 월별 변화율을 단순 평균한 "식품 가공 전체 흐름"을 쓴다. 판매
비중 가중 평균은 쓰지 않는다 — 그 비중은 D코드 하나로 특정되는 상품군들의
판매 비중인데, 이를 특정할 수 없는 상품군에 적용하면 구성이 비슷하다는 근거 없는
가정이 되기 때문이다. 이 선택의 한계:

- **"D코드 하나로 특정할 수 있는 상품군"은 라벨 기준의 근사 매핑이다.** D151은
  과일·채소·기름류까지 포함하는 넓은 코드이고, PREPARED FOODS와 D154는 D151과
  겹칠 수 있다. Favorita items에는 품목명이 없어(상품군, 익명 class 번호, 신선
  여부만 있음) 이 매핑을 검증할 수 없다.
- **단순 평균은 "구성을 모른다"를 반영한 것이 아니라 "네 코드 각 25%"라는 중립
  가정이다.** 실제 비중을 알 수 없어 단순 평균을 썼다.
- **PET SUPPLIES(사료 등 D153 성격 품목)는 비식품 범위 밖이라 제외 상품군이다.**
  D153은 제분·전분·사료를 포함하지만, 이 상품군은 우리 회사 제품 범위에 넣지
  않으므로 이 상품군을 통한 D153 대응은 만들지 않는다(전체 흐름 평균에는 D153이
  포함된다).

(나머지 TBD)
