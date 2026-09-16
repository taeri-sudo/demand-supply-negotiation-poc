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
못박은 이유대로 나중에 끼워 넣으면 이미 짠 negotiation 로직 전체를 다시 손봐야 한다.
반대로 `capacity_pools` 없이 시작하면 forecast↔supply_coordination이 "즉시 수락"만
반복하는 가짜 협상이 되어, 이 프로젝트의 핵심 목표(서로의 판단에 실제로 영향을 주는
다회 협상)를 처음부터 검증할 수 없다.

그래서 **State wrapper, capacity_pools, asyncio Queue처럼 "협상이 실제로 여러 라운드를
거쳐 값에 영향을 주는가"를 증명하는 데 필요한 최소 실물은 초기 마일스톤에서 바로 실물로
넣고, LLM 호출·실데이터 연동·SQLite 영속화·plan agent들의 정교한 판단 로직처럼
"무엇을 응답하는가"의 디테일은 뒤로 미룬다.**

## 전 마일스톤에 걸친 공통 규칙

아래 세 가지는 특정 마일스톤의 범위가 아니라, 여러 마일스톤에 걸쳐 처음부터 지켜야
나중에 재작업이 발생하지 않는 규칙이다.

**1. 검증 게이트는 edge를 하드코딩하지 않고 `interaction_protocol`/`role_permissions`를
읽어 라우팅한다(M2부터 적용).** "작성agent → 검증agent 큐 → 통과 시 목적지"라는
경로 자체를 `if edge == "forecast<->supply_coordination"` 같은 분기로 짜지 않고,
`interaction_protocol[]`에서 해당 edge의 검증agent role_tag·`max_rounds`·
`repeat_escalation_threshold`·`escalation_target`을 조회해 동작하는 일반화된 함수로
구현한다. 나중에 새 edge(예: plan agent 간 직접 상호작용, M5/M8에서 미확정으로 남긴 것)가
추가돼도 게이트 코드는 손대지 않고 `interaction_protocol`에 항목만 추가하면 되어야 한다.

**2. 규칙 기반 판단 스텁은 LLM 구조화 출력과 동일한 pydantic 스키마로 반환한다(M1부터
적용, M7에서 교체).** forecast의 후보 선택, analysis의 model_selection/data_source_basis
판단, supply_coordination의 우선순위 판단, plan agent의 feasibility 판단처럼 M7에서
LLM(②판단계층)으로 교체될 지점은, 지금 규칙 기반으로 구현하더라도 `{판단값, 근거}`
형태의 공통 pydantic 모델을 반환하게 만든다. M7에서는 이 스텁 함수의 내부 구현만
Gemini 구조화 출력 호출로 교체하고, 호출부(인터페이스)는 바뀌지 않아야 한다.

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

### M1 — 핵심 메커니즘 1: forecast ↔ supply_coordination 라운드 누적 협상 (최소 골격)
- `forecast`, `supply_coordination` 두 agent만 실물 구현. `analysis`는 하드코딩된
  candidate를 주는 스텁으로 대체(진짜 analysis는 M3).
- `capacity_pools`에 의도적으로 작은 값을 넣어 첫 제안이 즉시 수락되지 않게 강제.
- 종료조건은 단순화 버전(변화폭 임계치 이하)만 — `forecast_reliability` 게이트는 M6.
- `max_rounds` 소진 시 `escalation_records[]`에 기록 생성. validation 게이트는 아직 없음(M2).
- forecast의 후보 선택 판단은 **공통 규칙 2**에 따라 `{판단값, 근거}` pydantic 스키마로
  반환하는 규칙 기반 스텁으로 구현.

**검증**
- 수렴 시나리오: capacity 제약으로 몇 라운드에 걸쳐 제안이 낮아지며 수렴 → `round_history`의
  각 라운드 값이 실제로 달라지는지(서로의 판단에 영향을 줬는지) 확인.
- 비수렴 시나리오: capacity를 극단적으로 작게 줘서 `max_rounds` 소진 시 `escalation_records`가
  정확히 생성되는지 확인.
- `negotiation_log`가 실제 라운드 진행 순서와 일치하는지 확인.
- DESIGN.md 갱신: "진행 상황"에 M1 요약 추가, "검토 후 유지 확정"에 "capacity 제약 하
  다회 협상이 실제로 제안값을 변화시킴을 확인" 기록.

### M2 — 핵심 메커니즘 2: 검증 게이트 (interaction_protocol 기반 일반화 라우팅)
- 도메인 검증 agent 1개(예: `forecast_validation`)를 이벤트 트리거 워커풀로 구현.
- M1에서 직접 주고받던 흐름을 "작성자 → 검증agent 큐 → (통과 시) 원래 목적지"로 변경하되,
  **공통 규칙 1**에 따라 이 라우팅을 `interaction_protocol[]` 조회로 구현(edge 하드코딩 금지).
- 규칙 기반 판정만(LLM 판단은 M7, 단 **공통 규칙 2**에 따라 반환 스키마는 동일하게):
  capacity 총량 초과 여부, role_tag 쓰기 권한 여부. **대상 agent의 계산을 재현하지 않는
  원칙**을 지키는지 확인.
- `passed`/`flagged`/`check_failed` 3분기, `repeat_escalation_threshold`는 이력을
  훑어 그때그때 계산(별도 카운터 저장 안 함).

**검증**
- 정상 케이스: 유효한 제안이 통과 후 정상 전달되는지.
- `flagged` 케이스: capacity 초과 제안 → flagged → 재조정 → 재제출 → 최종 통과 재현.
- `repeat_escalation_threshold` 케이스: 같은 `routing_reason` 연속 발생 시 `max_rounds`
  소진 전에 escalation이 트리거되는지(M1의 "라운드 소진" 경로와 구분되는지).
- `check_failed` 케이스: 검증agent 내부 예외 강제 발생 → 재시도 후 escalation 확인.
- **일반화 검증**: `interaction_protocol[]`에 테스트용 더미 edge 항목 하나를 추가하는
  것만으로(게이트 코드 수정 없이) 그 edge가 같은 게이트를 통과하는지 확인 — 코드
  변경 없이 설정 추가만으로 새 edge가 동작함을 증명.
- DESIGN.md 갱신: "진행 상황"에 M2 요약, "검토 후 유지 확정"에 "검증 게이트는
  interaction_protocol 기반 일반 라우팅으로 구현, edge 추가 시 코드 변경 불필요" 기록.

### M3 — analysis ↔ forecast 핸드오프형 상호작용
- `analysis` agent 실물 구현(M1 스텁 제거). 데이터는 로컬 고정 샘플로 시작(Kaggle Store
  Item Demand 실연동은 M7).
- `data_source_basis`/`model_selection` 판단은 규칙 기반 스텁(**공통 규칙 2** 적용,
  LLM 교체는 M7).
- 핸드오프 시 재개 지점: `suspected_cause`가 무엇이든(model_selection이든
  data_source_basis든) **항상 데이터 수집 함수부터 재실행**하고, 캐싱은 두지 않는다
  (현재 데이터 수집 비용이 낮아 캐싱 이득이 적다는 판단). `suspected_cause` 값은
  기록용으로만 남기고 재개 지점 분기에는 사용하지 않는다.
- 이력 누적이 아니라 현재값 덮어쓰기임을 유지(`candidates`/`validation`은 매번 갱신).

**검증**
- `suspected_cause: model_selection`과 `suspected_cause: data_source_basis` 두 경우
  모두 데이터 수집 함수가 호출되는지(호출 카운트로 확인) — 분기 없이 항상 재실행됨을 증명.
- `suspected_cause`가 `analysis_agents[i]`의 기록(또는 `negotiation_log`)에 값만
  남고, 재실행 로직 자체에는 영향을 주지 않는지 확인.
- `candidates`/`validation`은 덮어써지지만 `negotiation_log`에는 이전 이력이 남는지.
- DESIGN.md 갱신: "진행 상황"에 M3 요약, "검토 후 유지 확정"에 "analysis 재실행은
  suspected_cause와 무관하게 항상 데이터 수집부터, 캐싱 없음" 기록.

### M4 — N개 회사로 확장 + 진짜 병행성 증명
- `analysis`/`forecast` 쌍을 회사별 N개(`asyncio.create_task()`)로 생성 — LangGraph
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

**검증**
- `required_stages`에서 빠진 plan agent가 전혀 호출되지 않는지(호출 카운트 0).
- logistics_plan을 강제 infeasible 처리했을 때 사슬을 거치지 않고 hub로 바로 돌아오는지
  (hub-and-spoke가 chain이 아님을 증명).
- 반복 infeasible로 `repeat_escalation_threshold`가 forecast/analysis/사람까지 확장되는
  경로 재현 — M2의 헬퍼가 이 엣지에서도 재사용되는지(코드 수정 없이 동작하는지).
- DESIGN.md 갱신: "진행 상황"에 M5 요약, "아직 결정 안 된 것"에 "plan agent 간 직접
  상호작용 여부 — M8에서 데이터 기반 판단 예정"이 유지되어 있는지 확인.

### M6 — forecast_reliability 영속화 + 우선순위 tier 2 + 협상 종료조건 완성
- SQLite 도입, walk-forward validation 구현.
- forecast↔supply_coordination 종료조건을 "변화폭 임계치 이하 **+** forecast_reliability
  게이트 통과"로 완성(M1 단순화 버전 대체). 우선순위 tier 2로 M4 스텁 제거.

**검증**
- 프로세스를 두 번 실행해 두 번째가 저장된 점수를 재계산이 아니라 로드하는지(호출 카운트로 구분).
- "변화폭은 임계치 이내이지만 reliability 게이트 미통과" 케이스에서 협상이 조기 종료되지
  않는지(AND 결합 검증).
- walk-forward 분할 로직을 알려진 기대값의 작은 시계열로 단위 테스트.
- DESIGN.md 갱신: "진행 상황"에 M6 요약, "검토 후 유지 확정"에 "협상 종료조건은
  변화폭+reliability AND 결합으로 확정" 기록, "미구현·todo 필드"에서 forecast_reliability
  관련 항목 제거.

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
