# 구현 마일스톤 순서 제안 (S&OP 멀티에이전트 POC)

> 이 문서 자체의 범위/검증 기준을 고치는 경우 이 파일을 직접 수정한다
> (DESIGN.md는 "진행 상황"만 요약).

## Context

CLAUDE.md, STATE_SCHEMA.md, AGENT_NODE_LIST.md, GRAPH_FLOW.md, DESIGN.md를 읽고
설계를 asyncio 기반 Python 코드로 옮기는 마일스톤 순서를 정리했다. DESIGN.md는
6개 섹션 전부 TBD 상태이고, 실질 아키텍처 정보는 STATE_SCHEMA/AGENT_NODE_LIST/
GRAPH_FLOW 세 문서에 분산되어 있다. 이 요청은 **코드 작성이 아니라 순서 설계**이므로,
아래는 실행 계획이 아니라 각 단계의 범위와 검증 방법을 정리한 참고 자료다.

## 선행 판단: 인프라를 먼저 만들지, Mock으로 흐름부터 볼지

`get_field`/`set_field` wrapper(role_permissions 검사 + set 시 큐 push를 짝짓는 것)는
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

아래 세 가지는 특정 마일스톤의 범위가 아니라, 여러 마일스톤에 걸쳐 처음부터 지켜야
나중에 재작업이 발생하지 않는 규칙이다.

**1. 검증agent는 edge를 하드코딩하지 않고 `interaction_protocol`/`role_permissions`를
읽어 동작한다(M2부터 적용).** 검증agent는 라우팅 권한이 없고 판정만 한다(GRAPH_FLOW.md
"검증agent" 참고) — `flagged`는 레코드를 만든 작성agent의
`validation_result.{role_tag}` 채널로 push(작성agent의 role_tag를 그대로 쓰므로
edge별 분기가 필요 없음), `check_failed`는 재시도 후 사람에게 push, `passed`는
받는 agent가 워커풀 성격(스스로 pull)인지 조율 성격(push 필요)인지에 따라
갈리는데 이것도 `if edge == "forecast<->supply_coordination"` 식으로 하드코딩하지
않고 역할별 성격 분류(role_tag 기준)로 판단한다. `interaction_protocol`의
`max_rounds`/`repeat_escalation_threshold`/`escalation_target`은 검증agent가
아니라 `flagged`를 받은 작성agent 자신이 재조정할지·상위로 확장할지 판단할 때
조회한다(검증agent가 그래프 구조 전체를 몰라도 되게 하기 위함). 나중에 새
edge(예: plan agent 간 직접 상호작용, M5/M8에서 미확정으로 남긴 것)가 추가돼도
검증agent 코드는 손대지 않고 `interaction_protocol`에 항목만 추가하면 되어야 한다.

**2. 규칙 기반 판단 스텁은 LLM 구조화 출력과 동일한 pydantic 스키마로 반환한다(M1부터
적용, M7에서 교체).** forecast(통합된 예측 agent)의 후보 선택·model_selection·
data_source_basis 판단, supply_coordination의 우선순위 판단, plan agent의
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
- STATE_SCHEMA.md의 8개 최상위 필드를 pydantic `BaseModel`로 전부 정의(구조는 문서
  그대로, 값은 비어 있어도 됨).
- `get_field`/`set_field` wrapper: `role_permissions[]` 화이트리스트 검사, `set_field`는
  값 기록과 동시에 해당 `asyncio.Queue`에 push.
- `capacity_pools`용 `asyncio.Lock`, ID/타임스탬프 헬퍼, `print()` 로깅 포맷.
- agent 로직 없음 — 순수 인프라.

**검증**
- 권한 없는 role_tag가 남의 필드에 `set_field`를 시도하면 거부되는지.
- `set_field` 호출 시 해당 큐에 정확히 1개 메시지가 들어오는지.
- 두 asyncio 태스크가 `capacity_pools.remaining_capacity`를 동시에 감소시키는 경쟁
  상황을 재현해, Lock 없이는 값이 틀리고 Lock을 걸면 정확함을 직접 입증.
- DESIGN.md 갱신: "진행 상황"에 M0 요약 추가.

### M1 — forecast(통합) → supply_coordination 최적화 배분 + 내부 되돌림 (최소 골격)
- `forecast`(analysis 통합, 데이터 수집→데이터 소스 판단→모델 선택→시나리오
  계산→후보 선택), `supply_coordination` 두 agent만 실물 구현. 데이터
  수집·시나리오 계산은 하드코딩된 candidate를 반환하는 스텁으로 대체(실물
  데이터/모델 로직을 어느 마일스톤에서 다룰지는 M3이 성립하지 않게 되며
  생긴 공백 — 아래 M3 참고, 재배치 필요).
- forecast의 후보 선택 판단은 **공통 규칙 2**에 따라 `{판단값, 근거}` pydantic
  스키마로 반환하는 규칙 기반 스텁으로 구현.
- 되돌림(핸드오프) 로직 — `suspected_cause`가 "데이터 소스 문제"/"모델 선택
  문제" 둘 중 무엇이냐에 따라 재개 지점이 갈리는지 구현(원래 M3 범위였으나,
  검증agent(M2)나 supply_coordination의 역방향 되돌림(M5) 같은 외부 트리거
  없이도 forecast agent 내부 로직만으로 독립 테스트 가능해 M1로 흡수 —
  candidate 선택의 판단3계층 분기와 같은 성격). 이력 누적이 아니라 현재값
  덮어쓰기임을 유지(`candidates`/`selected`/`validation`은 매번 갱신, 이력은
  `negotiation_log`).
- supply_coordination은 forecast가 선택한 candidate 값을 받아 우선순위 점수 산출
  (회사가 1개뿐이라 tier 1~4 경쟁 자체가 없음 — 실제 다회사 경쟁 로직은 M4) 후
  `allocation_candidate`를 1개 생성하는 **단방향 최적화**만 구현한다. 라운드/
  협상/escalation은 이 마일스톤 범위 밖이다 — GRAPH_FLOW.md "상호작용 세 가지
  유형"의 라운드 누적형(협상)은 공급망계획agent가 infeasible을 보냈을 때만
  열리는 예외 경로이고, 공급망계획agent 자체가 M5에서 구현되므로 이 예외
  경로도 M5에서 다룬다.

**검증**
- forecast가 candidate를 선택하면 그 값 그대로 `allocation_candidate`가 1개
  생성되는지 확인.
- `suspected_cause: "모델 선택 문제"`/`"데이터 소스 문제"` 두 경우 각각 재개
  지점이 정확히 갈리는지(호출 카운트로 확인 — "모델 선택 문제"는 데이터 수집
  함수 재호출 없이 모델 선택부터, "데이터 소스 문제"는 데이터 수집부터).
- `candidates`/`selected`/`validation`은 덮어써지지만 `negotiation_log`에는
  이전 이력이 남는지.
- `negotiation_log`에 candidate 선택 → `allocation_candidate` 생성 순서로
  이벤트가 남는지 확인.
- DESIGN.md 갱신: "진행 상황"에 M1 요약 추가.

### M2 — 핵심 메커니즘 2: 검증agent (interaction_protocol 기반 일반화, 라우팅 권한 없음)
- 도메인 검증 agent 1개(예: `forecast_validation`)를 이벤트 트리거 워커풀로 구현.
- M1에서 직접 최적화 결과를 쓰던 흐름을 "작성agent → 검증agent 큐 → 판정"으로
  바꾸되, **공통 규칙 1**에 따라 edge를 하드코딩하지 않고 `interaction_protocol[]`
  조회로 구현.
- 검증agent는 판정(`validation.status`)만 하고 라우팅은 안 함(GRAPH_FLOW.md
  "검증agent" 참고):
  - `passed`: 받는 agent가 워커풀 성격(스스로 pull)인지 조율 성격(push
    필요)인지에 따라 push 여부 갈림 — M1에는 조율 성격 소비자
    (`supply_coordination`)만 있어 이 분기는 실증 가능하지만, 워커풀 성격
    소비자(`procurement_plan` 등)는 M5 전까지 없으므로 더미 role_tag로
    단위 테스트.
  - `flagged`: 레코드를 만든 작성agent의 `validation_result.{role_tag}`
    채널로 push.
  - `check_failed`: 검증agent가 재시도 후 사람에게 push.
- 규칙 기반 판정만(LLM 판단은 M7, 단 **공통 규칙 2**에 따라 반환 스키마는 동일하게):
  capacity 총량 초과 여부, role_tag 쓰기 권한 여부. **대상 agent의 계산을 재현하지 않는
  원칙**을 지키는지 확인.
- `repeat_escalation_threshold`는 검증agent가 아니라 `flagged`를 받은 작성agent가
  이력을 훑어 그때그때 계산(별도 카운터 저장 안 함, 검증agent는 이 판단에
  관여하지 않음).

**검증**
- 정상 케이스: 유효한 제안이 `passed`면 조율 성격 소비자(`supply_coordination`)
  에게 push되는지.
- `flagged` 케이스: capacity 초과 제안 → flagged → 작성agent(`forecast`)의
  `validation_result.forecast` 채널로 push → 재조정 → 재제출 → 최종 통과 재현.
- `repeat_escalation_threshold` 케이스: 같은 `routing_reason` 연속 발생 시 `max_rounds`
  소진 전에 escalation이 트리거되는지(같은 edge의 `max_rounds` 소진 경로와
  구분되는지, 그리고 이 판단이 검증agent가 아니라 작성agent 쪽에서 일어나는지) —
  이 시점엔 실물 라운드형 edge가 아직 없으므로(M1은 단방향 최적화, 라운드형
  edge는 M5) 바로 아래 "일반화 검증"과 같은 더미 edge로 exchanges/round_history를
  합성해 확인한다.
- `check_failed` 케이스: 검증agent 내부 예외 강제 발생 → 재시도 후 escalation 확인.
- **일반화 검증**: `interaction_protocol[]`에 테스트용 더미 edge 항목 하나를 추가하는
  것만으로(검증agent 코드 수정 없이) 그 edge가 같은 흐름을 통과하는지 확인 — 코드
  변경 없이 설정 추가만으로 새 edge가 동작함을 증명.
- DESIGN.md 갱신: "진행 상황"에 M2 요약, "검토 후 유지 확정"에 "검증agent는
  판정만 하고 라우팅은 각 agent 자신의 역할로 분리, edge 추가 시 코드 변경
  불필요" 기록.

### M3 — 성립 안 함(analysis agent가 별도로 존재하지 않음)
analysis agent와 forecast agent가 하나로 통합되면서(2026-09-20, JOURNAL.md
참고) "analysis ↔ forecast 핸드오프형 상호작용"이라는 이 마일스톤 자체가
성립하지 않는다 — 되돌림은 이제 별도 agent 간 엣지가 아니라 통합된
forecast agent 내부의 재실행 로직이다.

**검토 결과**: 이 마일스톤이 검증하려던 핵심 내용(되돌림 시 재개 지점이
`suspected_cause`에 따라 정확히 갈리는지, 이력이 덮어써지는지)은 M1
범위로 흡수했다 — 핸드오프 자체가 검증agent(M2)나 supply_coordination의
역방향 되돌림(M5) 같은 외부 트리거 없이도, forecast agent 내부 로직만으로
독립적으로 테스트 가능하기 때문(candidate 선택의 판단3계층 분기와 같은
성격).

**흡수 안 된 부분(공백, 재배치 필요)**: M3에는 되돌림 검증 외에
`data_source_basis`/`model_selection` 판단의 **실물** 구현(M1 스텁 제거,
로컬 고정 샘플 데이터 사용)이라는 별개 범위가 있었는데, 이건 M1의
"내부 재실행 로직 검증"과 성격이 달라(스텁이 아니라 실물 판단 로직
자체를 만드는 일) 그대로 흡수하지 않았다. 이 실물 구현을 어느 마일스톤이
맡을지(M1 확장 vs 새 마일스톤 vs M4 이후로 미룸)는 아직 정하지 않았다 —
번호 재정렬과 함께 별도로 정리 필요.

### M4 — N개 회사로 확장 + 진짜 병행성 증명
- `forecast`(analysis 통합) agent를 회사별 N개(`asyncio.create_task()`)로 생성 — LangGraph
  Send API 대신 asyncio를 택한 핵심 근거(스텝 동기화 없는 진짜 병행)를 여기서 증명.
- 우선순위 tier 1(revenue_impact)·tier 3(aging 하한선)·tier 4(배분량 산출)만 구현,
  tier 2(forecast_reliability)는 SQLite가 필요하므로 M6까지 스텁(**공통 규칙 2** 적용).
- 여러 회사가 공유 `capacity_pools`를 실제로 경합하는 상황 구성.

**검증**
- 3개 이상 회사 동시 실행 시 한 회사가 응답을 기다리는 동안 다른 회사가 실제로 진행되는지
  (로그 순서가 인터리빙되는지) — 안 되면 asyncio 채택 근거 자체가 무너지므로 핵심 검증.
- capacity 부족 상황에서 revenue_impact 순으로 배분되는지.
- M0의 Lock이 다중 agent 부하에서도 `remaining_capacity`와 배분 합이 일치하는지.
- DESIGN.md 갱신: "진행 상황"에 M4 요약, "검토 후 유지 확정"에 "N개 회사 동시 실행 시
  실제 인터리빙 병행 확인(asyncio 채택 근거 검증됨)" 기록.

### M5 — supply_coordination ↔ procurement/production/logistics_plan (hub-and-spoke)
- 세 plan agent를 hub-and-spoke로 연결, 판단은 시드 고정 RNG로 infeasible을 발생시키는
  규칙 기반부터 시작(**공통 규칙 2** 적용, 업계 KPI 분포 샘플링 정교화는 M7).
- `required_stages`로 단계 스킵, `exchanges[]` 라운드 누적, `response_status` 종료조건.
- **plan agent 간 직접 상호작용(미확정 사항)은 건드리지 않고, 반드시 supply_coordination
  경유로만 구현.** M2에서 만든 일반화 게이트 덕분에, 향후 이 상호작용을 열기로 결정하면
  `interaction_protocol`에 항목만 추가하면 됨(공통 규칙 1).
- plan agent가 infeasible을 보내면 supply_coordination이 forecast로 역방향
  되돌림을 연다(GRAPH_FLOW.md "상호작용 세 가지 유형" 참고, 핸드오프형) —
  M1에서 구현한 되돌림 메커니즘(데이터 소스 문제/모델 선택 문제)을 그대로
  재사용한다. 트리거 조건만 procurement_plan 등의 infeasible 응답으로
  바뀔 뿐, 재실행 지점(데이터 소스 문제/모델 선택 문제 중 무엇인지)만
  정해지면 된다 — 새 라운드/escalation 로직은 필요 없다. `repeat_escalation_threshold`는
  이 엣지(핸드오프형, 라운드 없음)에는 해당 없음 — 여전히 라운드형인
  `supply_coordination↔procurement_plan` 등에서만 쓰인다.

**검증**
- `required_stages`에서 빠진 plan agent가 전혀 호출되지 않는지(호출 카운트 0).
- logistics_plan을 강제 infeasible 처리했을 때 사슬을 거치지 않고 hub로 바로 돌아오는지
  (hub-and-spoke가 chain이 아님을 증명).
- 반복 infeasible로 `supply_coordination↔procurement_plan`의
  `repeat_escalation_threshold`가 트립돼 forecast로의 핸드오프(또는 사람
  escalation)까지 확장되는 경로 재현 — `repeat_escalation_threshold` 자체는
  이 라운드형 엣지에서만 계산되고, forecast로의 핸드오프는 트리거만 될 뿐
  별도 카운트가 없는지 확인. M2의 헬퍼가 이 엣지에서도 재사용되는지(코드
  수정 없이 동작하는지)도 함께 확인.
- DESIGN.md 갱신: "진행 상황"에 M5 요약, "아직 결정 안 된 것"에 "plan agent 간 직접
  상호작용 여부 — M8에서 데이터 기반 판단 예정"이 유지되어 있는지 확인.

### M6 — forecast_reliability 영속화 + 우선순위 tier 2
- SQLite 도입, walk-forward validation 구현.
- 우선순위 tier 2로 M4 스텁 제거(STATE_SCHEMA.md "우선순위 구조" 참고 —
  supply_coordination의 배분 우선순위 산출에 쓰임).
- forecast_reliability 게이트를 실제로 어디에 붙일지는 **별도 확정 필요**
  — 원래 전제였던 forecast↔supply_coordination 라운드 수렴조건이
  무효화됐다(DESIGN.md "미구현/todo 필드" 참고). 이 마일스톤에서는
  산출·영속화 로직만 구현하고, 적용 위치 결정은 뒤로 미룬다.

**검증**
- 프로세스를 두 번 실행해 두 번째가 저장된 점수를 재계산이 아니라 로드하는지(호출 카운트로 구분).
- walk-forward 분할 로직을 알려진 기대값의 작은 시계열로 단위 테스트.
- 우선순위 tier 2 적용 시 revenue_impact가 임계치 이내로 비슷한 경우에만
  forecast_reliability로 순위가 조정되는지(tier 1이 확실히 갈리면 tier 2가
  안 쓰이는지).
- DESIGN.md 갱신: "진행 상황"에 M6 요약, "미구현·todo 필드"에서
  forecast_reliability 산출/영속화 항목 제거(적용 위치 미정 항목은 유지).

### M7 — LLM 판단 계층 통합 + 실데이터 연동 강화
- google-genai(Gemini) 구조화 출력을 판단③계층 중 ②(agent판단)가 필요한 지점에 연결:
  forecast tier 2, 애매한 model_selection, validator의 근거 타당성 판단, supply_coordination
  후보안 판단(순수 함수 여부는 여기서 실제로 구현하며 최종 결정 — 미확정 사항 해소).
  **공통 규칙 2** 덕분에 M1~M6에서 만든 스텁들의 반환 스키마가 이미 동일하므로, 내부
  구현만 Gemini 호출로 교체하고 호출부는 변경하지 않는다.
- Langfuse 트레이싱 연결, Kaggle Store Item Demand·SynDelay·업계 KPI 분포 샘플링을
  실제로 연동(M3/M5의 로컬 고정 샘플 대체).

**검증**
- 확실한 케이스는 LLM 호출 없이 규칙만으로 처리되고 애매한 케이스만 LLM이 호출되는지
  (Langfuse 트레이스로 확인).
- 구조화 출력이 `cast()` 없이 pydantic 모델로 그대로 파싱되는지.
- 스텁 → LLM 교체 시 호출부 코드(함수 시그니처, 반환 스키마 사용처)가 변경되지
  않았는지(공통 규칙 2 검증).
- supply_coordination 우선순위 점수 산출을 실제로 구현해보며 "순수 함수인지 판단 포함인지"를
  결정하고 근거를 기록.
- DESIGN.md 갱신: "진행 상황"에 M7 요약, "검토 후 유지 확정"에 "판단③계층 스텁→LLM 교체는
  인터페이스 변경 없이 구현체만 교체로 완료" 및 우선순위 점수 산출 방식 결론 기록,
  "아직 결정 안 된 것"에서 해당 항목 제거.

### M8 (선택/마무리) — end-to-end 시나리오 러너 + 남은 미확정 사항 정리
- N개 회사, 다회 라운드, 의도적 flag/escalation을 포함한 전체 시나리오 러너 작성.
- `negotiation_log` 통계를 근거로 "plan agent 간 직접 상호작용을 열지"를 데이터 기반으로
  판단(pm4py 전체 도입 없이 수작업 집계로도 가능한 최소 버전). 열기로 결정되면
  `interaction_protocol`에 새 edge 항목만 추가(공통 규칙 1 덕분에 게이트 코드 불변).
- DESIGN.md는 M0~M7에서 이미 마일스톤별로 갱신돼 왔으므로, M8에서는 "실무 전환 시
  고려사항" 섹션과 남은 TBD 항목(있다면)만 마무리로 채운다.

**검증**
- N≥3 회사, 최소 1회 검증 flag, 최소 1회 escalation을 포함한 시나리오가 예외 없이
  끝까지 실행되는지.
- `negotiation_log` 통계 근거로 미확정 사항에 결론을 도출했는지.
- DESIGN.md 6개 섹션에 더 이상 빈 TBD가 없는지(각 마일스톤에서 이미 채워졌는지 최종 확인).

## 참고: 문서 내 미확정(TBD) 사항과 배치

- DESIGN.md 6개 섹션 전체 TBD → M0부터 마일스톤마다 점진적으로 채움(공통 규칙 3), M8에서 마무리.
- 계획agent 간 직접 상호작용 여부 → M5에서는 우회, M8에서 데이터 기반 판단.
- supply_coordination 우선순위 점수 산출의 순수함수/판단 여부 → M7에서 해소.
- STATE_SCHEMA.md의 `execution_records[]` 분리 설계 → 이번 마일스톤 범위 밖(그래프 노드가
  아닌 외부 경계이므로 실행 layer를 agent화하기 전까지는 다루지 않음).
