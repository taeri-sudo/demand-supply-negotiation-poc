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

- **M2 2단계 (외부 경계 데이터 층, 진행 중인 M2의 일부)**: forecast agent가
  읽을 외부 데이터를 만들고 읽는 층을 구현했다. 변화율과 "식품 가공 전체 흐름"
  평균은 변화율 기준이 정해지는 4단계에서 만든다(아래 "아직 결정 안 된 것" 참고).
  - `favorita_mapping.py`: 상품군 33개를 D코드 하나(8개) / 식품 가공 전체 흐름(3개,
    D151-D154) / 제외(22개) 세 경우로 나눈 표(AGENT_NODE_LIST.md 매핑 표와 같은 값).
    전체 흐름은 대응하는 D코드 목록만 두고 평균은 만들지 않는다.
  - `favorita_loader.py`: train.csv를 청크로 읽어 지정한 매장·상품만 모으고
    (`scan_pos`), 매장을 고객사(`CUST-xx`)로, 상품을 `ITEM-nnn`으로 매핑한다.
    프로모션 여부는 true/false/null 세 상태를 유지하며 기록이 없는 2014년 4월
    이전을 false로 채우지 않는다. 월별 합산(`aggregate_monthly`)은 완전한 달만
    쓰고(2017년 7월까지), 프로모션 일/비프로모션 일/기록 없는 일 수를 따로 센다.
    월 단위 프로모션 여부는 기록 없는 날이 섞이면 "없음"이라고 단정하지 않고 null이다.
    정제는 하지 않는다(반품 음수도 그대로 — 3단계의 데이터 소스 판단 몫).
  - `ina_r_loader.py`, `ipc_loader.py`: INA-R 지수 시리즈(0은 0, 빈칸은 결측으로
    유지, 월은 헤더 문자열이 아니라 위치로 정하고 헤더와 어긋나면 오류)와 IPC
    지수 시리즈(이어붙인 파일, CCIF 코드는 문자열)를 읽는다.
  - `market_data.py`: 출처 등록(`INA_R_SOURCE`, `includes_own_sales: false`), 우리
    매출 차감(`exclude_own_sales`), 물가 보정(`deflate_market_index`). 차감은 출처 등록 때
    받은 `index_unit_amount`(지수 1포인트에 해당하는 시장 월 매출액)로 우리 월 매출을
    지수 포인트로 환산해 명목 지수에서 빼며, `includes_own_sales`가 true인데 이 값이
    없으면 등록 자체가 거부된다. 우리 매출이 시장 전체 이상이 되면 오류를 낸다.
    INA-R은 false라 실제 경로에서는 차감하지 않고 테스트에서만 true로 켠다. 보정은 지수 수준을 기준월 물가로 환산하며
    원본 시리즈는 바꾸지 않고 새 시리즈를 반환한다. 물가지수는 업종 단위(식품 가공은
    IPC 그룹 011, 비주류 음료는 012, 주류는 021)를 쓴다 — 주류 class에는 와인이
    없어 그룹 단위로 맞췄고, 상품군별 class 매핑은 하지 않았다.
  - `cost_inputs.py`: 적용 기간별 원가(`CostSchedule.at`이 계획 주기 시점에 유효한
    값을 고름)와 과잉·부족 비용. 신선식품이 남으면 제조원가 전체, 아니면 월
    보관비. 부족 비용은 (공급가 - 제조원가) + 결품 위약금이며, 위약금은 **주문
    금액(공급가)에 대한 비율**이다(확정). 기본값에서 식품 가공의 부족 비용은
    (1 - 0.76) + 0.03 x 1 = 0.27이다.
  - `order_generator.py`: 고객사별 (s, S) 정기발주 시뮬레이션으로 주문 이력을 만든다.
    고객사는 자기 POS의 직전 28일(또는 56일) 평균·표준편차로 수요를 추정한다.
    POS와 주문의 관계가 완벽하지 않도록 정책 파라미터의 시기별 변화, 점검일
    흔들림과 건너뜀, 수동 조정 주문, 프로모션 중 S 상향, 계약 시작일의 큰 첫
    주문(`is_first_order`)을 넣었다. 주문의 프로모션 표시는 직전 7일 안의 POS
    프로모션 판매일로 정하며 기록 시작 전은 null이다.
  - `sample_builder.py`/`external_data.py`: 고객사 6곳과 우리 회사 제품 범위의
    일별 POS에서 (고객사, item) 인스턴스 16개(smooth 7, seasonal 3, sparse 3,
    이력이 짧은 신규 계약 3)를 골라 주문 이력을 만들고 `data/generated/`에 고정
    파일로 저장했다(저장소에는 포함하지 않는다 — 아래 "데이터 출처". 같은 입력과 시드면
    바이트 단위로 같은 파일이 나옴을 확인). agent는 `external_data.py`의 읽기 함수로만 접근하며, 생성기 내부
    정책(`generator_params.json`)을 읽는 함수는 일부러 두지 않았다.
  - **합성 시리즈는 추가하지 않았다**: 예측기법 선택 검증에 필요한 계절성, 간헐수요,
    짧은 이력이 실제 샘플에 이미 있어(테스트로 확인) MILESTONES.md의 조건("샘플에 없으면
    추가")이 성립하지 않는다. 주문 데이터에는 `is_synthetic` 열을 두었고 지금은
    모두 false다.
  - 데이터 대체 가정(Favorita 매장을 고객사로 간주, 프로모션 기록 없음 구간, 우리
    회사 제품 범위, 생성 수주, INA-R 그룹 단위 사용과 우리 매출 미포함, 원가 입력
    기본값)은 AGENT_NODE_LIST.md "외부 경계"에 명시돼 있고 M2 완료 시 이 문서의
    M2 요약에 합친다. 이 단계에서 추가로 정한 점: 식별자는 GS1이 아니라 샘플 내부
    식별자를 쓴다(샘플에 대응하는 실제 코드가 없음), 고객사별 MOQ와 결품 위약률은
    `contracts.csv`에 둔다.
  - 의존성: INA-R xls를 읽기 위해 xlrd를 추가했다(requirements.txt 반영).
  - pytest 114건 통과(1단계 33건에 이 단계의 81건 추가). `data/generated/`가 없으면
    그 샘플을 읽는 14건은 건너뛰고 100건 통과 14건 건너뜀이 된다.

## 데이터 출처

원본 데이터는 저장소에 포함하지 않고(`data/raw/`는 .gitignore), 아래 출처에서 다시 받는다.
Favorita는 Kaggle 대회 규칙상 비상업적 용도로만 쓸 수 있고 재배포가 금지돼 있어, 원본과 이를 가공한 샘플은 저장소에 포함하지 않는다.

- **Favorita** (`data/raw/favorita/`): Kaggle "Corporación Favorita Grocery Sales
  Forecasting"(2017년 대회, 상품 단위). 다운로드에 Kaggle 로그인과 대회 규칙 동의가
  필요하다.
  https://www.kaggle.com/competitions/favorita-grocery-sales-forecasting/data
- **INA-R** (`data/raw/ina_r/`): INEC, 2003년 1월부터 2017년 12월까지 월별 시리즈.
  https://www.ecuadorencifras.gob.ec/ina-r-2017/
- **생성 샘플** (`data/generated/`): 생성 샘플은 저장소에 포함하지 않는다. 필요하면 raw
  데이터와 고정 시드로 sample_builder를 실행해 같은 파일을 만든다(생성기나 파라미터를
  바꾸면 결과가 달라지므로 다시 만들어야 한다). 실행은 저장소 루트에서
  `PYTHONPATH=src python -m sop.sample_builder`(PowerShell은 `$env:PYTHONPATH="src"; python
  -m sop.sample_builder`)이며, 원본 train.csv를 한 번 끝까지 읽어 몇 분 걸린다. 첫
  실행 결과를 `python -m sop.sample_builder <캐시.parquet>`로 캐시해 두면 다시 만들 때
  읽기를 건너뛴다. 이 파일을 읽는 테스트는 파일이 없으면 건너뛴다.
- **IPC** (`data/raw/ipc/`): INEC, 2026년 6월 판 압축 파일. 사용하는 파일은 압축 안의
  `Series IPC Empalmadas/ipc_ind_nac_reg_ciud_emp_clase_06_2026.xlsx`의 `1. NACIONAL`
  시트다.
  https://www.ecuadorencifras.gob.ec/indice-de-precios-al-consumidor/

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
두 경우로 나눠 만든다(매핑 표는 `favorita_mapping.py`에 구현됨, 평균은 4단계). 상품군 하나로 INA-R 3자리 그룹(D코드)
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

**M2 데이터 대체 가정의 한계 (우리 매출 차감)**: `includes_own_sales`가 true인 출처에서
우리 매출을 빼려면 매출액 규모가 필요한데 INA-R은 지수(2002=100)라서, 출처 등록 때
`index_unit_amount`(지수 1포인트에 해당하는 시장 월 매출액)를 받아 우리 월 매출을 지수
포인트로 환산한다. 이 값은 데이터에서 나오지 않는 가정값이고, 지수가 계절 조정값일
수 있어(INEC 방법론 문서의 식 8이 계절성 제거를 언급하나 파일에 표시는 없음) 월별
매출액으로 환산하는 것은 근사다. INA-R은 `includes_own_sales: false`라 실제 경로에서는
쓰이지 않는다. 시장 매출액을 직접 주는 출처로 바꾸면 환산 없이 매출액끼리 뺄 수 있다.

**식별자는 GS1이 아니라 샘플 내부 식별자를 쓴다 (실데이터 연동 시 교체)**:
AGENT_NODE_LIST.md "외부 경계"의 확정 원칙은 식별자를 GS1(상품은 GTIN, 거래처·장소는 GLN)
로, 필드를 EDI 문서(850 수주, 852 판매·재고) 기준으로 두는 것이다. 현재 샘플은 대응하는
실제 코드가 없어 `CUST-44`(Favorita 매장 번호), `ITEM-nnn`(Favorita 상품 번호) 같은 샘플
내부 식별자를 쓰고 필드 이름만 EDI에 대응하는 이름을 쓴다. 실데이터를 연동하는 M7에서
`external_data.py` 안의 변환(식별자를 GTIN/GLN으로 바꾸는 부분)을 추가해야 하며, agent
판단 로직은 식별자 문자열을 해석하지 않으므로 이 교체로 바뀌지 않아야 한다.

(나머지 TBD)
