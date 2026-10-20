# 구현 마일스톤 순서 제안 (S&OP 멀티에이전트 POC)

> 이 문서 자체의 범위/검증 기준을 고치는 경우 이 파일을 직접 수정한다
> (DESIGN.md는 "진행 상황"만 요약).

## Context

STATE_SCHEMA.md·AGENT_NODE_LIST.md·GRAPH_FLOW.md의 설계를 asyncio 기반 Python 코드로 옮기는 마일스톤 순서와, 각 단계의 범위와 검증 방법을 정리한다.

## 선행 판단: 인프라를 먼저 만들지, Mock으로 흐름부터 볼지

`get_field`/`set_field` wrapper(role_permissions 검사 + 상태가 바뀔 때 담당에게 신호 보내기)는
얇은 함수 하나에 불과하지만, CLAUDE.md가 "모든 State 접근은 wrapper를 통해서만"이라고
못박은 이유대로 나중에 끼워 넣으면 이미 짠 로직 전체를 다시 손봐야 한다.
반대로 `capacity_pools`를 M0에서 미루면, M4(여러 회사가 실제로 같은
`capacity_pools`를 경합)와 M5(procurement_plan 등이 실제로 infeasible을 응답해
라운드가 연장)에 가서야 State 스키마와 Lock 보호를 다시 설계해야 하고, 그
시점에는 이미 M1~M3에서 그 스키마를 전제로 짠 로직을 다시 손봐야 한다.

그래서 **State wrapper, capacity_pools, asyncio Queue처럼 "여러 agent/회사가
실제로 서로의 판단·자원에 영향을 주는가"를 M4(N개 회사의 capacity 경합)·M5
(plan agent와의 실제 feasibility 라운드)에서 증명하는 데 필요한 최소 실물은
M0에서 미리 실물로 넣고, LLM 호출·실데이터 연동·SQLite 영속화·plan agent들의
정교한 판단 로직처럼 "무엇을 응답하는가"의 디테일은 뒤로 미룬다.**

## 전 마일스톤에 걸친 공통 규칙

아래 세 가지는 특정 마일스톤의 범위가 아니라, 여러 마일스톤에 걸쳐 처음부터 지켜야 나중에 재작업이 발생하지 않는 규칙이다.

**1. 검증agent는 edge를 하드코딩하지 않고 `interaction_protocol`/`role_permissions`를
읽어 동작한다(M3부터 적용).** 검증agent는 라우팅 권한이 없고 판정만 하며 다음 agent에게
전달하지 않는다(GRAPH_FLOW.md "검증agent" 참고). 판정을 쓰면 기록이 "판정됨" 상태가 되어 작성agent의
작성agent의 채널(`role_tag`)로 신호가 간다(edge별 분기가 필요 없다). `passed`면
작성agent가 원래 다음 agent에게 직접 보내고, `failed`면 작성agent가 처리하며, `error`는 바로 escalation 기록을 만든다(시스템 대기).
`if edge == "forecast<->supply_coordination"` 식으로 하드코딩하지 않는다. `interaction_protocol`의
`max_exchanges`/`repeat_escalation_threshold`/`escalation_target`은 검증agent가
아니라 `failed`를 받은 작성agent 자신이 재조정할지·상위로 확장할지 판단할 때
조회한다(검증agent가 그래프 구조 전체를 몰라도 되게 하기 위함). 나중에 새
edge(예: M5의 회의 엣지)가 추가돼도
검증agent 코드는 손대지 않고 `interaction_protocol`에 항목만 추가하면 되어야 한다.

**2. 규칙 기반 판단 스텁은 LLM 구조화 출력과 동일한 pydantic 스키마로 반환한다(M1부터
적용, M7에서 교체).** forecast(통합된 예측 agent)의 가정 정의·데이터 소스 판단·
통계기법 선택·가정 선택, supply_coordination의 배분 판단, plan agent의
feasibility 판단처럼 M7에서 LLM(②판단계층)으로 교체될 지점은, 지금 규칙 기반으로
구현하더라도 `{판단값, 근거}` 형태의 공통 pydantic 모델을 반환하게 만든다. M7에서는
이 스텁 함수의 내부 구현만 Gemini 구조화 출력 호출로 교체하고, 호출부(인터페이스)는
바뀌지 않아야 한다.

**3. DESIGN.md는 마일스톤이 끝날 때마다 그 자리에서 갱신한다(M8까지 몰아서 채우지
않는다).** 각 마일스톤 완료 시 "진행 상황" 섹션에 해당 마일스톤에서 구현·검증한
내용을 요약으로 추가한다(pytest로 확인한 사실, 구현 중 스스로 내린 설계 판단 포함).
"검토 후 현재 구조 유지로 확정" 섹션에는 이 중 사용자와 실제로 논의한 뒤 원래 구조를
유지하기로 확정한 결정만 별도로 추가한다 — 구현 검증 결과 자체는 이 섹션 대상이
아니다. "아직 결정 안 된 것"/"미구현·todo 필드" 섹션도 마일스톤 진행에 따라 항목을
지우거나 추가한다. 각 마일스톤이 끝날 때, DESIGN.md 갱신과 함께 JOURNAL.md에도 그
마일스톤에서 실제로 있었던 대안 비교/기각 논거를 기록 기준(CLAUDE.md)에 따라 남긴다.

## 마일스톤

### M0 — State 스켈레톤 + 접근통제 wrapper (인프라, agent 없음)
- STATE_SCHEMA.md의 최상위 필드 전체를 pydantic `BaseModel`로 전부 정의(구조는 문서 그대로, 값은 비어 있어도 됨).
- `get_field`/`set_field` wrapper: `role_permissions[]` 화이트리스트 검사, 여러 곳을 한 번의 State 갱신으로 쓰는 `update_state`.
  신호(`asyncio.Queue`)는 기록의 상태가 바뀔 때 담당에게 자동으로 보낸다(M3에서 상태 규칙을 구현하고, M0은 권한 검사와 큐만 만든다).
- `capacity_pools`용 `asyncio.Lock`, ID/타임스탬프 헬퍼, `print()` 로깅 포맷.
- agent 로직 없음 — 순수 인프라.

**검증**
- 권한 없는 role_tag가 남의 필드에 `set_field`를 시도하면 거부되는지.
- `set_field` 호출 시 해당 큐에 정확히 1개 메시지가 들어오는지.
- 두 asyncio 태스크가 `capacity_pools.remaining_capacity`를 동시에 감소시키는 경쟁 상황을 재현해, Lock 없이는 값이 틀리고 Lock을 걸면 정확함을 직접 입증.
- DESIGN.md 갱신: "진행 상황"에 M0 요약 추가.

### M1 — forecast(통합) → supply_coordination 최적화 배분 + 내부 재실행 (최소 골격)
- `forecast`(analysis 통합, 데이터 수집→데이터 소스 판단→모델 선택→가정별 요청량
  계산→후보 선택), `supply_coordination` 두 agent만 실물 구현. 데이터
  수집·요청량 계산은 하드코딩된 candidate를 반환하는 스텁으로 대체(실물 데이터/모델 로직은 M2에서 다룬다).
- forecast의 후보 선택 판단은 **공통 규칙 2**에 따라 `{판단값, 근거}` pydantic 스키마로 반환하는 규칙 기반 스텁으로 구현.
- 재실행 로직 — `suspected_cause`가 "데이터 소스 문제"/"모델 선택
  문제" 둘 중 무엇이냐에 따라 재개 지점이 갈리는지 구현(원래 별도 마일스톤
  범위였으나, 검증agent의 불합격 판정(M3)이나 supply_coordination의 역방향 send-back(M5) 같은 외부 트리거
  없이도 forecast agent 내부 로직만으로 독립 테스트 가능해 M1로 흡수 —
  candidate 선택의 판단3계층 분기와 같은 성격). 이력 누적이 아니라 현재값
  덮어쓰기임을 유지(`candidates`/`selected`/`validation`은 매번 갱신, 이력은 `role_logs`).
- supply_coordination은 forecast가 선택한 candidate 값을 받아 우선순위 점수 산출
  ((회사,item) 인스턴스가 1개뿐이라 tier 1~4 경쟁 자체가 없음 — 실제 다인스턴스 경쟁 로직은 M4) 후
  `allocation_candidate`를 1개 생성하는 **단방향 최적화**만 구현한다. 라운드/
  협상/escalation은 이 마일스톤 범위 밖이다 — GRAPH_FLOW.md "상호작용 세 가지
  유형"의 라운드 누적형(협상)은 공급망계획agent가 infeasible을 보냈을 때만
  열리는 예외 경로이고, 공급망계획agent 자체가 M5에서 구현되므로 이 예외 경로도 M5에서 다룬다.

**검증**
- forecast가 candidate를 선택하면 그 값 그대로 `allocation_candidate`가 1개 생성되는지 확인.
- `suspected_cause: "모델 선택 문제"`/`"데이터 소스 문제"` 두 경우 각각 재개
  지점이 정확히 갈리는지(호출 카운트로 확인 — "모델 선택 문제"는 데이터 수집
  함수 재호출 없이 모델 선택부터, "데이터 소스 문제"는 데이터 수집부터).
- `candidates`/`selected`/`validation`은 덮어써지지만 재실행 항목(`rerun`)의 `payload`에 덮어쓰기 전 값이 남는지.
- 합쳐 읽은 `negotiation_log`(`seq` 순)에 candidate 선택 → `allocation_candidate` 생성 순서로 이벤트가 남는지 확인.
- DESIGN.md 갱신: "진행 상황"에 M1 요약 추가.

### M2 — forecast agent 실물 판단 로직 구현 (스텁 제거)
- M1의 하드코딩 스텁을 실물 판단 로직으로 교체한다. 입력·내부 단계·재실행은
  AGENT_NODE_LIST.md forecast agent, 필드는 STATE_SCHEMA.md `forecast_records` 기준으로 구현한다.
- **스키마 교체**: M1의 임시 스키마를 STATE_SCHEMA.md의 확정 스키마로 바꾼다
  (`forecast_agents`→`forecast_records`, `company_id` 필수,
  `data_source_basis`→`data_sources`/`excluded_sources`/`cleaning`,
  `model_selection`→가정별 `method_values`, `candidates`→`assumptions`,
  `selected`→`scenario`, `suspected_cause` 문자열→구조, 알림용
  `interaction_protocol` 항목 추가). **공통 규칙 2**는 이 확정 스키마 기준으로 적용한다.
- 모든 판단은 규칙 기반(LLM 연동은 M7). 가정 정의도 규칙이 채운다 (`defined_by: "rule"`).
- 최소 구매 약정은 이미 맺은 계약 수량으로 supply_coordination의 입력이며 M4·M5에서 처리한다.
- 재실행: M1의 재개 지점 분기를 유지하되, 판단 함수에 의심되는 원인이
  전달되도록 확장하고 `assumption` 의심되는 원인(가정 정의부터 재실행)를 추가한다.
- 사람 escalation의 State 반영: 계산된 가정이 하나도 없을 때(`scenario` 없음), 가정 선택 ③, 재실행으로 가정이 모두 제외된 경우(`options_exhausted`)를
  `escalation_records`(`agent_id`·`reason`·`rationale`)로 남기고, 미처리 escalation 기록이 있는 동안 supply_coordination으로
  보내지 않는다(AGENT_NODE_LIST.md "사람 escalation 세 경우"). `interaction_protocol`에는 `forecast->human_manager`
  의 `selection_unresolved` 항목만 둔다. 사람 입력의 처리는 M4.
- 가정 검증 조건은 순수 함수로 구현한다(검증agent 연결은 M3).
- 규칙이 확신하지 못하는 "애매함" 표시 기준은 구현 시 제안받아 검토한다.
- 데이터: 로컬 고정 샘플(AGENT_NODE_LIST.md forecast agent "입력"·"외부 경계"·
  sales_channel 기준 — Favorita 일부 + 수주 생성기로 만든 주문 이력 + INA-R).
  주문 이력에는 첫 주문·프로모션 기간 표시를 넣는다. 통계기법 선택 검증에
  필요한 특성(계절성, 간헐수요, 짧은 이력)이 샘플에 없으면 가공 시리즈를
  추가하고 합성임을 표시. 전체 규모 연동은 M7.
- 시장 데이터 물가 보정, `includes_own_sales`에 따른 차감 로직을 구현한다. 원가 입력에서
  과잉·부족 비용을 계산하는 로직(`cost_inputs.py`)도 만들었지만 forecast는 쓰지 않는다
  (비용 기반 요청량 결정은 forecast 밖이고 위치 미정 — DESIGN.md).
- category_trend 가정은 시장 변화율이 우리 수요(같은 item의 POS) 변화율에 주는 영향을
  통계로 추정해 정한다(계절 성분을 뺀 로그 수준의 전월 차분, 시차 하나로 고정, 단순
  회귀와 자기상관에 강건한 95% 신뢰구간 — AGENT_NODE_LIST.md 1단계).
- 식품 가공 전체 흐름은 D151-D154 월별 변화율의 단순 평균으로 만든다(INA-R
  공개 시리즈에 그룹별 비중이 없어 가중 평균은 쓰지 않음, 한계는 DESIGN.md
  참고). 하나의 D코드로 특정되는 상품군은 그 D코드 변화율을 그대로 쓴다.
- 예측기법 라이브러리는 statsforecast(pandas 2.3.3 고정)를 쓴다.

**검증**
- 재실행: 같은 스냅샷에서 `suspected_cause`가 주어지면 직전과 **다른** 결과가
  나오는지(의심되는 원인별 대응표의 각 행), 의심되는 원인 없이 같은 입력이면 **같은** 결과가 나오는지(결정론).
- 재실행, 수단 없음: 원인이 없어진 가정(`assumption`), 소스를 제외할 수 없는 `orders` 오염, 남은 기법 구성이 없는
  `method_selection`, 재실행의 대상 가정 중 재실행해도 입력이 직전 재실행과 같은 가정이 제외되고
  `excluded_assumptions`에 의심되는 원인(`{type}:{issue}`)가 제외 이유로 기록되는지. 입력이 바뀐 가정과 대상이 아닌 가정은 남는지.
- 재실행, `assumption`: 대상이 `assumption_id`의 가정이고 그 가정의 원인이 모두 제외되어 `excluded_drivers`에
  의심되는 원인으로 기록되는지, 다른 가정은 영향받지 않는지.
- 재실행, 범위: 대응(보강, 소스 제외, 시점 적용, 기법 제외, 입력 불변 규칙에 따른 제외)이 대상 가정에만 적용되고
  대상이 아닌 가정은 학습 시리즈와 입력이 바뀌지 않고 제외되지도 않는지(예: 시장 소스가 `irrelevant`일 때 시장 소스를
  쓰지 않는 기본 가정, 시장 소스를 지정한 `insufficient`). `source`가 없는 `insufficient`와 `method_selection`이 모든
  가정을 대상으로 하는지. 소스를 쓰는 가정이 근거로 선언했거나 학습 시리즈에 값을 낸 가정으로 판정되는지.
- 재실행, `data_source`: `insufficient`는 대상 가정마다 독립으로 끝까지 보강하고 더 얹을 데이터가 없으면 제외하는지,
  `contaminated`는 해당 소스가 `excluded_sources`로 제외되고 `orders` 오염이면 모든 가정이 제외되는지, `outdated`는
  `data_sources`의 `use_from`에 기록되는지, `irrelevant`는 소스가 `excluded_sources`로 제외되는지. `orders`의
  `irrelevant`와 `use_from` 없는 `outdated`가 오류로 거부되는지.
- 2단계 판단 결과: `irrelevant`와 `outdated` 두 분기가 같은 이름의 `issue`로 남는지.
- 재실행, `method_selection`: 대상이 모든 가정이고 한 번의 재실행 처리 동안 직전 기법 구성이 가정 안에서만 누적해
  제외되는지, 다른 가정이 같은 기법을 계속 쓰는지, 남은 구성이 없으면 그 가정이 제외되는지, 누적용 State 필드가 없는지.
- 보강 우선순위: 가정마다 독립으로, 월마다 (1) 그 가정의 원인·전제가 근거로 삼는 소스, (2) 같은 item의 다른 소스,
  (3) 상위 단위 데이터 순서로 쓰는지(첫 실행 포함).
- 재실행으로 기본 가정까지 제외됐을 때: 재실행 전에 계산된 `scenario`가 있었으면 `options_exhausted`이고
  `scenario`와 `assumptions`가 그 값 그대로인지, 재실행 전부터 없었으면 `no_computable_assumption`이고 `scenario`가 `null`인지.
- 재실행한 결과의 `validation`이 비워지는지.
- 재실행이 무한히 돌지 않는지: 재실행 한 번마다 대상 가정의 입력이 바뀌거나 그 가정이 제외되어 유한한 횟수에 끝나는지(반복 횟수 세기 없이).
- 데이터 소스: 이력이 짧은 인스턴스(예: 신제품이 막 들어온 고객사·item)는
  보강 소스가 추가되고, 충분한 인스턴스는 기본 데이터만 쓰는지. 오염은
  `cleaning`에, 관련 없는 소스는 `excluded_sources`에, 구조 변화·첫 주문은 `use_from`에 기록되는지.
- 데이터 특성이 다른 두 인스턴스가 가정마다 고른 기법 구성(method_values의 method)이 달라지는지
  (하드코딩된 스텁과 달리 실제 입력에 반응하는지).
- 가정: 연동 근거(시장 변화율이 우리 수요 변화율에 주는 영향의 95% 신뢰구간이 0을 포함하는지)에
  따라 category_trend 원인을 단 가정이 남거나 제외되는지(`no_significant_effect`, `excluded_drivers`에 원인과
  영향받은 가정이 기록되는지). 프로모션 이력이 없으면 `event` 전제가 모든 가정에서 빠지고 이유가 기록되는지.
  가정 정의가 가정과 근거만 선언하는지(기법별 값과 `value` 비어 있음). 가정마다 기법별 값이 가정 안에만 있고
  `value`가 하나인지, 발생 가능성이 `occurrence_likelihood`로만 표현되는지.
- 통계기법: 원인이나 전제가 있는 가정에서는 반영할 수 있는 기법만 남고 나머지는 제외되는지, 같은 시계열이라도
  가정마다 기법 가중치가 다른지(가정마다 따로 잰 과거 정확도), 그 가정이 성립했던 기간의 표본이 부족하면 가중치가
  균등이고 "애매함"인지, 맞는 기법이 하나도 없는 가정이 `excluded_assumptions`에 기록되는지.
- 기본 가정: 확정 프로모션 전제를 설명변수로 받는 기법이 하나도 계산되지 않으면 기본 가정이 전제 없이 우리
  주문 이력만으로(시계열 기법 포함) 계산되는지, 전제를 반영하지 못했다는 사실과 이유가 `excluded_drivers`에
  `no_applicable_method`로 기록되고 "애매함"이 표시되는지, 기본 가정이 전제 유무와 상관없이 항상 후보인지.
- 가정 안 합치기·선택과 가정 선택: 기법이 하나/지배적/그 외, 가정이 하나/값이 거의 같음/1등이 뚜렷함/값이 많이 다름에 따라
  결과(`derivation`)와 "애매함"·사람 escalation 표시가 갈리는지.
- 가정 검증 조건 함수가 위반 케이스를 잡아내고, 요청량이 같아도 데이터나 기법이 다르면 다른 가정으로 보는지.
- 최소 구매 약정이 forecast의 입력·가정·알림에 없는지(driver 목록에 약정이 없고, 가정 선택이 약정 인자를 받지 않는지).
- 시장 데이터: 물가 보정 후에도 원본 매출액이 그대로 남는지. `includes_own_sales: true`로 설정한 테스트에서 우리 매출이 차감되는지.
- 원가(`cost_inputs.py` 단위 검증, forecast는 쓰지 않음): 신선식품이 남으면 과잉 비용이 제조원가 전체가 되는지, 결품 위약률을
  고객사별로 바꾸면 부족 비용이 바뀌는지, 적용 기간이 다른 원가 값을 넣으면 주기에 맞는 값을 쓰는지.
- 프로모션 기록 없음: "기록 없음" 구간이 "없음"과 구분되는지, 프로모션 효과가
  기록 있는 구간으로만 계산되는지, 며칠 튄 날은 정제되고 한 달 이상 올랐다 돌아온 구간은 `use_from`으로 잘리는지.
- 요청량을 만들 수 없을 때: 계산된 가정이 하나도 없는 인스턴스에서 `scenario`·`selection_basis`가 `null`이고,
  `no_computable_assumption` escalation 기록(`agent_id`, `rationale` 포함, `intervention`, `open`)이 하나만 만들어지며,
  supply_coordination으로 신호가 가지 않고 기록이 "검증 대기" 상태가 되지 않아 검증agent에게도 신호가 가지 않는지. 다른 인스턴스는 정상 진행하는지.
- `scenario.value = 0`(계산된 정상 값, escalation 없음)과 `scenario = null`(escalation 있음)이 구분되는지.
- 사용할 주문이 없는 인스턴스(주문 0건, `use_from` 후 빈 경우)가 예외 없이 위 경로로 가는지.
- ③: 값이 많이 다르고 1등 가정도 뚜렷하지 않을 때 `selection_unresolved` escalation 기록이 만들어지고 `scenario`에 중간값이 있으며
  `selection_basis`가 `"rule"`이고 보내지지 않는지. 값이 많이 달라도 1등이 뚜렷하면 escalation 기록이 만들어지지 않는지.
- 같은 스냅샷으로 다시 돌려도 같은 `(agent_id, reason)`의 미처리 escalation 기록이 중복 생성되지 않는지.
- `interaction_protocol`에 `forecast->human_manager`(`selection_unresolved`) 항목이 있고 나머지 두 reason에는
  항목이 없는지. 조회 키가 `(edge, escalation_trigger)`로 동작하는지.
- 월별 합산: 마지막 불완전한 달이 제외되는지.
- 반환 스키마가 공통 pydantic 모델(`{판단값, 근거}`)인지(공통 규칙 2, M7 교체 대비).
- DESIGN.md 갱신: "진행 상황"에 M2 요약, 데이터 대체 가정(Favorita store→고객사
  매핑, 프로모션 기록 없음 구간, 우리 회사 제품 범위와 상품군 매핑, 생성 수주
  데이터, INA-R 그룹 단위 사용과 우리 매출 미포함, 원가 입력 기본값과 근거) 명시.
  JOURNAL.md에 스키마 변경 이유 기록.

### M3 — 검증agent (interaction_protocol 기반 일반화, 라우팅 권한 없음)
- 검증agent의 공통 틀과 `forecast_validation` 하나를 이벤트 트리거 워커풀로 구현한다. 검증 규칙은 agent마다 그
  agent를 만드는 마일스톤에서 더한다(M4는 supply_coordination 배분안, M5는 plan agent). 검증agent가 무엇을
  보는지는 AGENT_NODE_LIST.md 검증agent 절이 정의한다.
- `forecast_validation`의 규칙은 M2에서 순수 함수로 만든 가정 검증 조건 4개(`no_evidence`, `value_out_of_range`,
  `not_distinct`, `double_counted`)이고 `assumption` 유형의 이유만 판정에 싣는다. `no_evidence`와
  `double_counted`는 지목할 수 있는 원인을 `drivers`에 넣는다(STATE_SCHEMA.md `suspected_cause`).
- M1에서 직접 최적화 결과를 쓰던 흐름을 "작성agent → 검증agent 큐 → 판정"으로 바꾸되, **공통 규칙 1**에 따라
  edge를 하드코딩하지 않고 `interaction_protocol[]` 조회로 구현한다. 검증agent는 State와 이번 주기 스냅샷만 읽는다
  (과거 월별 요청량은 forecast와 같은 기준일의 스냅샷에서 읽는다).
- 검증agent는 판정(`validation.status`)만 쓰고 라우팅도 전달도 안 함(GRAPH_FLOW.md "검증agent" 참고). 신호는 기록의 상태가 바뀔 때
  담당에게 자동으로 가고(GRAPH_FLOW.md "신호 규칙"), 받는 쪽은 기록을 읽어 자기 상태일 때만 일한다. 보류에 들어갈 때는 로그 항목,
  escalation 기록, 보류 기록을 보류 전용 함수가 한 번의 `update_state`로 쓴다(이미 보류 중인 인스턴스에는 오류):
  - `passed`: forecast가 카드로 다음 agent를 골라 `forward_to`에 써서 직접 넘긴다(기록이 "전달됨"이 되어 신호가 가며, 이미
    "전달됨"인 기록은 다시 넘기지 않는다).
  - `failed`(불합격 판정): forecast가 `suspected_causes` 전체를 한 번의 재실행으로 처리한다(AGENT_NODE_LIST.md forecast "재실행").
  - `error`: 검증 절차가 예외로 끝나면 재시도 없이 바로 `validation_error` escalation
    기록을 만들고 그 인스턴스만 멈춘다.
- agent 카드(STATE_SCHEMA.md 8번 절): 경로 위 agent마다 카드(`agent_cards`)를 둔다. 지금 카드는 forecast(`produces`: 검증을 통과한 수요
  예측, `known_agents`: `[supply_coordination]`)와 supply_coordination(`accepts`: 검증을 통과한 수요 예측, `known_agents`: `[]`)이다.
- 다음 agent 선택(GRAPH_FLOW.md "다음 agent 선택"): `passed` 뒤에 forecast가 `known_agents` 중 `accepts`가 자기 `produces`와 맞는 agent를
  후보로 고른다. 후보가 하나면 `forward_to`에 쓰고, 없으면 `no_next_agent`, 여럿이면 `multiple_next_agents` escalation(둘 다 `warning`)이다.
  `forward_to`에 `known_agents` 밖의 agent를 쓰면 거부한다.
- 받는 쪽 반송(GRAPH_FLOW.md "받는 쪽 반송"): "전달됨" 기록을 받은 agent가 자기 카드의 `accepts`와 맞지 않으면 `send_back`에 `misrouted`를
  써서 되돌린다. 작성agent는 재실행하지 않고 다음 agent 선택만 다시 하며, 그 기록에 대해 반송한 agent를 후보에서 뺀다. 값 문제 `send_back`은
  재실행한다. `send_back` 필드와 "되돌려짐" 상태 계산은 M3에서 만들고, supply_coordination이 값 문제로 쓰는 것은 M5다.
- 채널은 agent의 `role_tag` 하나이고, 받는 쪽은 기록의 상태를 읽어 할 일을 정한다.
- 처리되지 않은 `intervention` escalation 기록이 있는 인스턴스는 검증agent로도 supply_coordination으로도 넘기지
  않는다(GRAPH_FLOW.md "사람 입력과 검증의 무결성").
- escalation 긴급도(`emergency`/`warning`)는 코드 안의 reason→긴급도 대응표로 정한다(STATE_SCHEMA.md 7번 절).
- 규칙 기반 판정만(LLM 판단은 M7, 단 **공통 규칙 2**에 따라 반환 스키마는 동일하게). **대상 agent의 계산을 재현하지
  않는 원칙**을 지키는지 확인.
- 로그 구조(STATE_SCHEMA.md 4번 절): 역할별 로그 `role_logs`가 원본이고 `negotiation_log`는 `seq` 순으로 합쳐 읽는 함수의
  결과다. 사건 이름은 정해진 것만 쓰고 값은 필드와 `payload`에 담는다. 각 역할은 자기 로그에만 쓴다.
- forecast 실행 실패: 첫 실행이든 재실행이든 예외로 끝나면 forecast 로그에 단계와 예외 내용을 남기고 재시도 없이 바로
  `forecast_run_error` escalation 기록을 만들고 그 인스턴스만 멈춘다(AGENT_NODE_LIST.md
  forecast "실행 실패").
- 라운드 누적형 엣지용 같은 이유의 연속 횟수 헬퍼: `failed`를 받은 작성agent가 그 edge의
  `exchanges`/`round_history`를 훑어 같은 이유가 연속 몇 번인지 그때그때 계산한다(별도 카운터 저장 안 함,
  검증agent는 관여하지 않음). `repeat_escalation_threshold`는 라운드 누적형 엣지에서만 쓴다.

**검증**
- 신호 규칙(GRAPH_FLOW.md "신호 규칙"): 기록의 상태가 바뀔 때만, 그 상태의 담당 agent의 `role_tag` 채널로만 신호가 가는지(정상 완료는 검증agent, 판정은
  작성agent, 전달됨은 `forward_to`의 agent, 되돌려짐은 작성agent, 보류 진입과 실행 오류는 human_manager), 로그만 쓸 때는 신호가
  없는지, 받는 쪽이 자기 상태가 아닌 기록의 신호(늦게 도착한 신호 포함)를 받으면 아무것도 하지 않는지, 유효한 forecast 기록이
  `passed`면 forecast가 넘겨서 `allocation_candidate`가 만들어지는지, 이미 "전달됨"인 기록을 다시 넘기지 않는지, `send_back`을 쓰면
  작성agent에게 신호가 가고 재실행 결과를 쓰면 기록이 "검증 대기"로 돌아가는지.
- `update_state`: 여러 필드와 로그 항목을 한 번의 State 갱신으로 쓰는지, 쓰는 도중 예외가 나면(forecast 정상 기록, 보류, 전달, 후보안
  생성, 판정 passed/failed/error, 재실행 기록, 실행 오류 보류) State가 이전 그대로이고 신호도 가지 않으며 예외가 삼켜지지 않고
  올라가는지.
- 보류 쓰기: 로그 항목, escalation 기록, 보류 기록을 한 번의 `update_state`로 쓰고 이미 보류 중인 인스턴스에는 오류인지,
  escalation 기록의 `log_seq`가 같은 갱신에서 쓴 로그 항목(`forecast_run_error`는 예외 내용, 그 밖에는 escalation 시점의 값)을
  가리키는지.
- 보류 중인 인스턴스는 첫 실행과 재실행 모두 실행하지 않고 건너뛴 사실만 forecast 로그에 남기는지(기록, escalation 기록 불변).
- `failed` 케이스: 가정 검증 조건 위반 → failed → forecast가 자기 기록의 판정을 확인해 `suspected_causes`
  전체로 재실행 → 재제출 → 최종 통과 재현. `no_evidence`·`double_counted`는 `drivers`가
  지목되고 `value_out_of_range`·`not_distinct`는 null인지.
- 한 기록에 여러 위반이 겹칠 때 `suspected_causes`가 목록으로 나가고, 재실행이 가장 앞 재개 지점에서 시작해 단계
  순서대로 적용하며 수단 없음 비교를 가정마다 한 번만 하는지.
- `error` 케이스: 검증 내부 예외 강제 발생 → 재시도 없이 바로 `validation_error` escalation 기록
  (`trigger_edge`, `agent_id`, `mode`) 확인. 그 인스턴스만 멈추고 다른 인스턴스는 진행하는지.
- 처리되지 않은 `intervention` escalation 기록이 있는 인스턴스는 "사람 대기" 상태라 검증agent도 supply_coordination도 일하지 않는지.
- 다음 agent 선택: forecast가 카드로 supply_coordination을 고르는지, 후보가 0개이면 `no_next_agent`, 여럿이면 `multiple_next_agents` escalation이
  나는지, `known_agents` 밖의 `forward_to`가 거부되는지.
- 받는 쪽 반송: `misrouted` 반송을 받으면 재실행 없이 다음 agent만 다시 고르는지, 반송한 agent가 후보에서 빠지는지, 남은 후보가 없으면
  `no_next_agent` escalation이 나는지.
- `repeat_escalation_threshold` 케이스: 같은 이유가 연속 발생하면 `max_rounds` 소진 전에 상위로 확장하는 판단이
  나오는지(`max_rounds` 소진 경로와 구분되는지, 그리고 이 판단이 검증agent가 아니라 작성agent 쪽에서 일어나는지).
  이 시점엔 실물 라운드 누적형 edge가 아직 없으므로(라운드 누적형 edge는 M5) 아래 "일반화 검증"과 같은 더미 edge로
  exchanges/round_history를 합성해 확인한다.
- **일반화 검증**: `interaction_protocol[]`에 테스트용 더미 edge 항목 하나를 추가하는 것만으로(검증agent 코드 수정
  없이) 그 edge가 같은 흐름을 통과하는지 확인 — 코드 변경 없이 설정 추가만으로 새 edge가 동작함을 증명.
- escalation 긴급도 대응표에 코드가 만드는 모든 reason이 있는지.
- 재실행 뒤 합쳐 읽은 `negotiation_log`에서 덮어쓰기 전 값을 찾을 수 있는지, 합친 결과가 `seq` 순인지, 다른 역할의 로그에
  쓰면 거부되는지.
- forecast 실행 실패: 예외가 나면 바로 `forecast_run_error` 기록이 만들어지며 forecast 기록은 이전 값 그대로이고 그 인스턴스만
  멈추는지.
- 검증agent의 쓰기 권한이 `forecast_records[*].validation`뿐이어서 다른 필드를 쓰면 거부되는지.
- DESIGN.md 갱신: "진행 상황"에 M3 요약, "검토 후 유지 확정"에 "검증agent는 판정만 하고 라우팅은 각 agent 자신의
  역할로 분리, edge 추가 시 코드 변경 불필요" 기록.

### M4 — 생산라인 단위 조율 + N개 (회사,item) 인스턴스
- **N개 인스턴스**: forecast를 (회사,item)마다 `asyncio.create_task()`로 여러 개 실행하고, 여러 회사·item이 같은 라인의 capacity를
  실제로 경합하는 상황을 만든다. 이 마일스톤은 여러 인스턴스가 번갈아 진행될 때 결과가 맞는지만 확인한다. 한 인스턴스가 기다리는
  동안 다른 인스턴스가 실제로 진행되는 병행성 증명은 실제 기다림(LLM 응답 대기)이 생기는 M7에서 한다.
- **State 변경**(STATE_SCHEMA.md): `allocation_candidates`→`allocations`(라인마다 확정된 결론 하나), `capacity_pools`의 식별자를
  `line_id`로, `forecast_records`에서 `pool_key` 제거, `forecast_records.selection_basis`에 `expiry` 추가, `validation_settings` 추가
  (최상위 필드 9개), `role_permissions`에 `a`(이어서 쓰기 — `role_logs`에 적용), `interaction_protocol`의 `max_rounds`→`max_exchanges`
  (선택 필드)와 항목 정리(5번 절의 세 항목), `escalation_records`의 `target_role`·`line_id`·`deadline`·`status`·`resolution`.
- **기록 상태**(GRAPH_FLOW.md "신호 규칙"): 일곱 가지 상태(업무 대기, 시스템 대기, 되돌려짐, 전달됨, 완료, 판정됨, 검증 대기)와 검증 면제
  통과(`exempt_passed`, `selection_basis`가 `human`/`expiry`). forecast의 `forecast_run_error`·`validation_error`·`no_next_agent`·
  `multiple_next_agents`는 `system_operator` 담당이다.
- **외부 데이터**(AGENT_NODE_LIST.md "외부 경계"): 수주(앞으로 납기가 올 수주 포함 — 수주 생성기 확장, 고객사 재고 정책이 받지 못한
  주문을 가진 양에 넣는지 확인), 사람이 정한 수량, 계약 조건(최소 구매 약정·공급 보장 물량·계약 단위), 재고·생산 중 물량(고정 파일
  스텁), `line_assignments`(M4는 `primary`만). 라인의 `total_capacity` 초기값(가동률 0.8 임시).
- **supply_coordination**(AGENT_NODE_LIST.md): 수요 합치기, 공급 가능량, 배분 규칙(미납분 → 계약 하한 → 1개당 마진 → 비율 → 계약 단위),
  하한 초과·강한 묶임 초과 escalation, 약정 차이 알림, 결론 쓰기와 실패 처리. 묶임(`peg`)은 테스트에서 지정한다. 배분 판단은 **공통 규칙 2**를 따른다.
- **조율 흐름**(GRAPH_FLOW.md "조율 흐름"): 정기 조율 시작 규칙(교체 가능한 class), 정기 조율 전 변경 무시, 입력 고정과 마지막 순서로
  얹기, 이어지는 조율의 시점(즉시 / 하루 한 번 정해진 시각), 스케줄러.
- **검증**: `allocation_validation`(배분 합계 ≤ 공급 가능량)을 M3의 공통 틀에 규칙만 더해 구현한다. 검증 대상과 시점은 `validation_settings`에서 읽는다.
- **escalation 처리**(STATE_SCHEMA.md 7번 절): reason 대응표(담당·긴급도·시한 초과 처리), 사람의 처리 함수(escalation 기록과 값을
  받는 기록을 한 번의 `update_state`로, 판정 칸은 비워 두고 `selection_basis`로 표시), 입력 제한, 시한 초과 처리.
- **신호 전달**: 확인 응답 방식(GRAPH_FLOW.md "동시성 모델").

**검증**
- 여러 인스턴스(같은 회사의 다른 item, 같은 라인을 함께 쓰는 수요, 혼자 쓰는 라인을 섞음)가 함께 돌 때 배분 결과·`remaining_capacity`·
  로그가 맞는지, 실행 순서를 바꿔도 같은 배분이 나오는지. M0의 Lock이 이 부하에서도 `remaining_capacity`와 배분 합을 일치시키는지.
- 정기 조율: 그 라인 인스턴스 중 처리 중인 것이 있으면 시작하지 않고, 업무 대기·시스템 대기만 남으면 시작해 그 인스턴스를 빼고
  배분하는지. 다른 라인을 기다리지 않는지. 검증 불합격으로 재실행 중인 인스턴스의 결과가 같은 조율에 드는지.
- 정기 조율 전에 온 변경 신호는 아무 일도 하지 않고, 그 변경이 정기 조율에 함께 들어가는지.
- 조율 중 들어온 값이 결론 직전의 확인에서 마지막 순서로 얹히고, 확인 뒤 들어온 값은 바로 이어지는 조율로 가는지. 결론이 한 번만 써지는지.
- 이어지는 조율: 사람의 처리 결과·사람이 정한 수량·급한 변경은 진행 중인 조율 직후에, 일반 자동 변경은 정해진 시각에 조율되는지.
  조율을 열 때 그 라인에 모인 변경이 모두 들어가고 다른 라인 것은 들어가지 않는지. 강한 묶임은 바뀌지 않고 약한 묶임만 옮겨지는지.
- 수요 합치기: 같은 (고객사, item)에서 예측보다 수주가 작으면 예측, 크면 수주가 수요가 되는지(같은 수요가 두 번 세어지지 않는지).
- 배분 규칙: 미납분(납기가 오래 지난 순) → 계약 하한(약정의 이번 주기 몫 = 잔여 ÷ 남은 개월 수, 공급 보장 물량 중 큰 쪽) → 1개당
  마진 → 요청량 비율 순으로 나뉘는지. 계약 단위로 내림되고 자투리가 배분되지 않는지.
- 하한 초과: 미납분과 계약 하한의 합이 공급 가능량을 넘으면 그 라인만 `commitments_exceed_capacity` 업무 대기가 되고 이전 결론이
  유지되며 다른 라인은 진행하는지. `interaction_protocol`의 모드를 `notice`로 바꾸면 코드 변경 없이 멈추지 않는지. 강한 묶임 합계가
  capacity 감소로 공급 가능량을 넘으면 `pegged_exceed_capacity`인지.
- 약정 차이: 같은 차이에서 구속력 없는 약정만 알림이 나가는 경우를 재현하고, 알림 후에도 진행이 멈추지 않는지. 구속력 없는 약정이
  하한으로 쓰이지 않는지.
- 배분 결론 검증: 배분 합계가 공급 가능량을 넘으면 `failed`이고, 입력이 바뀌었으면 다시 조율, 같으면 `allocation_invalid`
  (`system_operator`)인지. `passed`면 "완료"이고 신호가 가지 않는지.
- escalation 처리: 사람이 forecast의 reason을 처리하면 요청량이 `scenario`에 들어가고 `selection_basis: "human"`이며 `validation`이
  비어 있는데도 기록이 "판정됨"이 되어 검증agent를 다시 거치지 않고 다음 agent로 넘어가는지(검증agent에게 신호가 가지 않는지). 입력
  제한을 어긴 값이 거부되는지. 시한이 지나면 reason별 처리(중간값으로 진행, 재실행 전 값으로 진행, 예측 수요 없이 진행)대로 되고
  `status: "expired"`이며, 값으로 진행한 경우 `selection_basis: "expiry"`이고 `validation`이 비어 있는 채로 넘어가는지. 사람의 처리나
  시한 초과로 넘어간 기록이 그 뒤 재실행되면 `selection_basis`가 새로 쓰여 다시 "검증 대기"가 되는지. `system_operator` 담당 reason은
  시한 없이 시스템 대기로 남는지. 업무 담당자가 `system_operator` 담당 기록을 읽지 못하는지.
- 받는 쪽 반송(사람의 처리로 넘어간 기록): `misrouted` 반송 뒤 다시 고를 때 같은 기록인지를 처리 시각(`resolution_ts`)으로 알아보고,
  판정 시각(`validation_ts`)과 섞어 비교하지 않는지. 로그 `forwarded`·`misrouted_send_back`에 두 시각 중 하나만 채워지는지.
- 신호 전달: 처리 중 강제로 실패시킨 신호가 확인 시간 뒤 다시 나타나 처리되고, 처리를 확인한 신호는 다시 오지 않는지. 같은 신호가
  두 번 와도 결과가 같은지.
- `a` 권한: `role_logs`에 이어서 쓰기만 되고 기존 항목을 고치는 쓰기가 거부되는지.
- escalation 대응표에 코드가 만드는 모든 reason이 있는지.
- DESIGN.md 갱신: "진행 상황"에 M4 요약, 가동률 0.8 임시값과 재고·생산 중 물량 스텁을 데이터 대체 가정으로 명시.

### M5 — supply_coordination ↔ procurement/production/logistics_plan (회의)
- **회의 구조를 먼저 설계한다**(GRAPH_FLOW.md "공급망계획agent 연결 구조"): 회의 기록(`meetings`)의 끝나는 조건과 동시 쓰기를 먼저
  확정한다. 설계할 수 없으면 같은 문서에 적힌 동시 요청 방식으로 한다. 그다음 `meetings`를 State에 더한다.
- 세 plan agent를 구현한다. 판단은 시드 고정 RNG로 "요청대로는 안 됨"과 대안을 발생시키는 규칙 기반부터 시작한다(**공통 규칙 2**,
  업계 KPI 분포 샘플링 정교화는 M7).
- plan agent 카드(`response_type: round_accumulation`)와 supply_coordination의 `produces`·`known_agents`, `validation_settings` 행(plan
  agent `per_record`, supply_coordination `on_conclusion`), plan agent 검증 규칙(`procurement_validation` 등)을 M3의 공통 틀에 더한다.
  GRAPH_FLOW.md 엣지 표에 plan agent 행을 더한다.
- 회의 범위 규칙(문제가 생긴 자원을 쓰는 수요들), `interaction_protocol`의 회의 엣지 항목(`max_exchanges`, `repeat_escalation_threshold`).
- 충돌 판단: plan agent의 대안을 비용으로 비교한다(AGENT_NODE_LIST.md supply_coordination agent "M5에서 더하는 것").
- production_plan: 준비량(안전재고 입력은 `forecast_uncertainty`, 임시), 생산 최소 단위, 라인 배정과 전환(`line_assignments` 갱신,
  다음 조율부터), 전용 라인 성질(`production_lines`), `peg` 결정. procurement_plan·logistics_plan: 여러 라인이 함께 쓰는 자원 관리.
- 월 예측을 고객사의 과거 주문 시점 패턴으로 주 단위로 나눠 납기를 두는 함수(판단 없는 계산).
- plan agent가 요청대로는 안 된다고 했고 문제가 수요 값에 있으면 supply_coordination이 forecast 기록의 `send_back`을 써서 역방향
  send-back을 보낸다(핸드오프형). M1~M3의 재실행 메커니즘(`suspected_causes`)을 그대로 재사용하고, 재실행은 같은 조율 안에 든다.
- 공급 보장 물량을 채우지 못하면 escalation — escalation이 sales_channel "조정 불가" 통지보다 먼저 나가는 순서.
- 구속력 없는 약정의 이행 불확실성 반영 규칙을 plan agent 응답과 연결해 확정한다.
- 같은 소스에 반대되는 대응이 함께 올 때의 우선 규칙을 정한다(M3은 이유 목록을 단계 순서대로 모두 적용하고 서로 반대되는 대응의
  우선순위는 정하지 않는다 — AGENT_NODE_LIST.md forecast "재실행").

**검증**
- 회의: 참여 agent가 다른 agent의 통과된 기록을 보고 자기 판단을 바꾸는 경우를 재현하는지. 상관없는 agent가 아무것도 쓰지 않아도
  회의가 끝나는지. 읽은 뒤 새 기록이 생긴 상태에서의 쓰기가 거절되고, 새로 더해진 기록만 읽고 다시 쓰는지. 요청·응답 수가
  `max_exchanges`를 넘으면 escalation되는지.
- 검증 시점: plan agent의 기록은 기록마다 검증되고, supply_coordination의 회의 기록은 검증 대상이 아니며 결론만 검증되는지. 통과하지
  않은 기록을 다른 agent가 판단에 쓰지 않는지.
- 충돌 판단: 아끼는 비용이 어기게 되는 비용보다 크면 대안을 받아들이고 아니면 원래 요청을 유지하는지.
- 같은 이유의 반복으로 `repeat_escalation_threshold`가 트립돼 forecast send-back이나 사람 escalation으로 확장되는 경로 재현. M3의 헬퍼가
  코드 수정 없이 회의 기록에서도 재사용되는지.
- production_plan의 준비량 계산(수요 + 미납분 − 재고 − 생산 중 물량 + 안전재고)과 생산 최소 단위로 남은 양이 다음 계산에 재고로
  반영되는지.
- 공급 보장 미충족 escalation이 sales_channel 통지보다 먼저 나가는지.
- DESIGN.md 갱신: "진행 상황"에 M5 요약.

### M6 — 실제 예측 정확도 영속화 + 안전재고 입력 교체
- 주기마다 실제로 낸 요청량(`scenario`)과 그 뒤 실제 주문을 쌓아 (고객사, item)별 예측 정확도를 계산하고 SQLite에 영속화한다
  (walk-forward validation — 과거 데이터를 시간순으로 잘라, 그 시점까지의 데이터로 다음 구간을 예측 → 실제값과 비교 → 기준 시점을
  한 칸씩 밀며 반복).
- production_plan의 안전재고 입력을 `forecast_uncertainty`(임시)에서 이 값으로 바꾼다. 이 값은 배분 순서에는 쓰지 않는다.

**검증**
- 프로세스를 두 번 실행해 두 번째가 저장된 값을 재계산이 아니라 로드하는지(호출 카운트로 구분).
- walk-forward 분할 로직을 알려진 기대값의 작은 시계열로 단위 테스트.
- 안전재고 입력을 바꿔도 production_plan의 호출부가 바뀌지 않는지(**공통 규칙 2**와 같은 방식).
- DESIGN.md 갱신: "진행 상황"에 M6 요약, "미구현·todo 필드"에서 해당 항목 정리.

### M7 — LLM 판단 계층 통합 + 실데이터 연동 강화 + 병행성 증명
- google-genai(Gemini) 구조화 출력을 판단③계층 중 ②(agent판단)가 필요한 지점에 연결:
  가정 정의(`defined_by: "agent_judgment"`, 실패 시 규칙 fallback)·애매한 통계기법 선택·애매한 가정 선택, validator의 근거 타당성 판단,
  supply_coordination의 배분에서 규칙이 확신하지 못하는 경우와 회의에서 비용 비교가 애매한 경우(배분에 판단이 섞이는지는 여기서
  실제로 구현하며 결정). **공통 규칙 2** 덕분에 M1~M6에서 만든 스텁들의 반환 스키마가 이미 동일하므로, 내부 구현만 Gemini 호출로
  교체하고 호출부는 변경하지 않는다.
- 다음 agent 선택에서 후보가 여럿이면 LLM이 카드의 `description`을 읽고 고른다(같은 반환 스키마로 M5의 규칙 스텁을 교체).
- LLM 호출의 일시적 오류(호출 한도 초과, 응답 시간 초과)에 대한 재시도는 LLM 호출 단위로 둔다. 검증agent의 검증 절차와
  forecast 실행이 예외로 끝나는 시스템 오류는 재시도하지 않고 바로 escalation한다.
- **병행성 증명**: 한 인스턴스가 LLM 응답을 기다리는 동안 다른 인스턴스가 실제로 진행되는지 확인한다(LangGraph Send API 대신
  asyncio를 택한 근거). 통계 계산(statsforecast)은 CPU를 쓰는 동기 코드라 계산하는 동안 다른 태스크가 진행되지 않으므로, 그 영향을
  보고 계산을 스레드나 프로세스로 뺄지 정한다.
- Langfuse 트레이싱 연결, 고객사 POS 전체 데이터·생성 수주 데이터·시장 데이터·
  SynDelay·업계 KPI 분포 샘플링을 전체 규모로 연동(M2/M5의 로컬 고정 샘플 대체).

**검증**
- 확실한 케이스는 LLM 호출 없이 규칙만으로 처리되고 애매한 케이스만 LLM이 호출되는지(Langfuse 트레이스로 확인).
- 구조화 출력이 `cast()` 없이 pydantic 모델로 그대로 파싱되는지.
- 스텁 → LLM 교체 시 호출부 코드(함수 시그니처, 반환 스키마 사용처)가 변경되지 않았는지(공통 규칙 2 검증).
- 3개 이상 인스턴스를 함께 실행할 때 한 인스턴스가 LLM 응답을 기다리는 동안 다른 인스턴스가 진행되는지(같은 회사의 서로 다른 item
  인스턴스 포함, 로그 순서가 번갈아 나오는지) — 안 되면 asyncio 채택 근거 자체가 무너지므로 핵심 검증.
- supply_coordination의 배분을 실제로 구현해보며 "순수 함수인지 판단 포함인지"를 결정하고 근거를 기록.
- DESIGN.md 갱신: "진행 상황"에 M7 요약, "검토 후 유지 확정"에 "판단③계층 스텁→LLM 교체는 인터페이스 변경 없이 구현체만 교체로
  완료", "N개 인스턴스의 실제 병행 확인(asyncio 채택 근거 검증됨)", 배분 판단 방식의 결론 기록, "아직 결정 안 된 것"에서 해당 항목 제거.

### M8 (선택/마무리) — end-to-end 전체 실행 러너 + 남은 미확정 사항 정리
- N개 회사, 여러 번의 회의, 의도적 flag/escalation을 포함한 전체 실행 러너 작성.
- `negotiation_log` 통계를 근거로 `interaction_protocol`의 기준값(`max_exchanges` 등)과 회의 범위가 적절한지 판단(pm4py 전체 도입 없이
  수작업 집계로도 가능한 최소 버전). 바꾸기로 하면 `interaction_protocol` 값만 바꾼다(공통 규칙 1 덕분에 코드 불변).
- DESIGN.md는 M0~M7에서 이미 마일스톤별로 갱신돼 왔으므로, M8에서는 "실무 전환 시
  고려사항" 섹션과 남은 TBD 항목(있다면)만 마무리로 채운다.

**검증**
- N≥3 회사, 최소 1회 검증 flag, 최소 1회 escalation을 포함한 시나리오가 예외 없이 끝까지 실행되는지.
- `negotiation_log` 통계 근거로 기준값에 결론을 도출했는지.
- DESIGN.md 6개 섹션에 더 이상 빈 TBD가 없는지(각 마일스톤에서 이미 채워졌는지 최종 확인).

## 참고: 문서 내 미확정(TBD) 사항과 배치

- DESIGN.md 6개 섹션 전체 TBD → M0부터 마일스톤마다 점진적으로 채움(공통 규칙 3), M8에서 마무리.
- 회의 구조의 끝나는 조건과 동시 쓰기 → M5 시작 때 먼저 확정.
- supply_coordination 배분 판단의 순수함수/판단 여부 → M7에서 해소.
- 실제 병행성 증명과 통계 계산의 스레드·프로세스 분리 여부 → M7.
- STATE_SCHEMA.md의 `execution_records[]` 분리 설계 → 이번 마일스톤 범위 밖(그래프 노드가
  아닌 외부 경계이므로 실행 layer를 agent화하기 전까지는 다루지 않음).
- 공급 보장 물량 미충족 시 escalation ↔ sales_channel 통지 순서 → M5에서 구현.
- capacity_pools 집계 수준의 적절성 → M5에서 negotiation_log로 실증 확인.
- 가동률 0.8의 근거 → 근거 자료를 찾으면 초기값만 다시 계산.
