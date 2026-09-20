# Step 2 Agent/노드 목록

State에 여기저기서 참조만 되던 `role_tag`를 여기서 실제로 선언한다.
Step1 원칙4(판단이 필요한 노드만 Agent)로 각 항목을 분류.
변수/태그명은 영어(snake_case), 설명은 한글.

동시성 모델은 asyncio(단일 프로세스) — 아래 "지속 태스크"는 각자 독립된
asyncio 태스크로 서로 안 막히고 진행, "이벤트 워커풀"은 트리거될 때만
반응. 상세는 STATE_SCHEMA.md "동시성 모델" 절 참고.

## 전체 구조 요약

```
forecast agent(회사별 1개, 총 N개, role_tag: forecast) — 지속 태스크
   (데이터 수집→데이터 소스 판단→모델 선택→시나리오 계산→후보 선택을
    한 agent 내부 단계로 수행 — 원래 analysis/forecast 두 agent였으나
    되돌림 지점이 "데이터 소스 문제"/"모델 선택 문제" 둘로 충분해 통합)
        ↕  (신호 기반, 평소 정방향 최적화 / 예외 시에만 역방향 핸드오프)
supply_coordination agent(1개, role_tag: supply_coordination) — 지속 태스크
   ↕procurement_plan   ↕production_plan   ↕logistics_plan  (각 1개, 지속 태스크)
   (hub-and-spoke — 셋 다 직접 연결, 사슬 아님. 순서는 의존관계에 따른
    호출 순서일 뿐, 건너뛰기/역방향 되돌림 모두 구조적으로 가능)
        ↓
실제 조달/생산/배송 (그래프 노드 아님, 외부 경계 — sales_channel과 같은 성격)

validation agent(들) — 일감은 이벤트 트리거·무기억 워커풀 방식으로 받고,
판정(`passed`/`flagged`/`check_failed`)만 State에 쓴다 — 라우팅 권한은
없고, push 여부는 받는 agent 성격(워커풀=pull, 조율=push)에 따른
기계적 규칙일 뿐이다(상세는 GRAPH_FLOW.md "검증agent" 참고). human_manager(들) —
escalation 발생 시에만 반응, 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 되돌릴지는 interaction_protocol이
규정.
```

**동시성 모델**: asyncio 단일 프로세스(Docker/Redis 없이 시작) — 각 지속
태스크가 서로 안 막히고 독립적으로 진행, in-process 신호(asyncio.Queue)로
소통. "라운드"는 그래프 스텝이 아니라 신호 교환으로 구현되며, 모든 분기가
끝나야 다음으로 넘어가는 제약(Pregel 방식의 한계) 자체가 없음. 상세는
GRAPH_FLOW.md·AGENT_NODE_LIST.md 참고.

## 지속 태스크 (판단·협상을 계속 담당)

물리적 실행이 아니라 **계획(약속·커밋) 판단**만 하므로 여기 속한다(아래
"외부 경계"와 구분).

### forecast agent (Agent, 회사별 1개, 총 N개, role_tag: `forecast`)
데이터 수집부터 후보 선택까지 한 agent가 담당한다 — 원래 analysis agent와
forecast agent로 나뉘어 있었으나, 분리 이유였던 "되돌림 지점이 다르면
agent도 분리"가 되돌림 지점을 재검토한 결과 두 값으로 충분해져(아래
"되돌림" 참고) 더 이상 성립하지 않아 통합했다.

내부 단계(순서대로):
- **데이터 소스 판단**(Agent, 판단) — 자사 과거 실적만으로 충분한지, 아니면
  유사 업종/유사 프로모션을 시행한 타사 데이터(외부 데이터)까지 참고해야
  하는지 결정(신제품·신규 프로모션처럼 자사 이력이 없거나 부적합한 경우).
  "회사 단위 인스턴스"가 곧 "그 회사 데이터만 참고"를 뜻하지 않음
- 데이터 수집 (함수) — 위 판단에 따라 자사/외부 실적 조회. **데이터 소스:
  Kaggle Store Item Demand(실데이터)**
- **모델 선택** (Agent, 판단) — 어떤 통계기법을 쓸지(계절성/간헐수요 등에 따라)
- 예측 시나리오(a/b/c) 계산 (함수) — 선택된 모델로, 파라미터만 다르게
- 표준 라이브러리(statsmodels 등) 함수를 기본값으로 그대로 호출, 자체
  알고리즘 개발/하이퍼파라미터 튜닝은 안 함(정교화 범위 밖)
- **후보 선택** (Agent, 판단) — 위에서 계산된 a/b/c 중 하나 선택, 판단3계층
  적용. 이 선택값은 평소엔 supply_coordination에게 단방향으로 전달돼
  최적화 배분에 쓰인다(GRAPH_FLOW.md "상호작용 세 가지 유형"의 최적화 참고).

**되돌림(핸드오프)**: `suspected_cause`는 두 값만 쓴다 — "데이터 소스
문제"(데이터 수집부터 재실행)/"모델 선택 문제"(모델 선택 단계부터
재실행, 이후 시나리오 계산·후보 선택 전부 다시 돎). "후보 선택만 다시"
라는 세 번째 값은 두지 않는다 — 모델 선택부터 다시 돌리면 새 후보가
나와 후보 선택도 자연히 다시 이뤄지므로 별도 재개 지점이 필요 없다.

**후보 재선택은 supply_coordination의 역방향 되돌림에서도 일어난다**
(Agent, 판단) — 공급망계획agent(procurement_plan 등)의 infeasible 신호로
supply_coordination이 역방향 되돌림을 시작했을 때만 조건부로 발생하는
예외 경로다(GRAPH_FLOW.md 엣지 표의 "supply_coordination → forecast
(역방향, 예외)" 참고). 값을 주고받으며 조금씩 좁히는 협상이 아니라
"선택이 틀렸을 수 있으니 다시 하라"는 재실행 지시이고, `suspected_cause`는
위 두 값 중 하나로만 온다 — 같은 되돌림 메커니즘을 그대로 재사용한다.

### supply_coordination agent (Agent, 1개, role_tag: `supply_coordination`)
- **후보안(allocation_candidates) 생성·선정** (Agent, 판단)
- 우선순위 점수 산출 (함수 — 4단 구조 계산식, 가중치 조정 등은 판단 여지
  있음, 세부 설계 시 재확인 필요)
- **공급망계획agent 문제 신호 처리** (Agent, 판단) — 자체 재조정으로 풀지,
  위/옆(forecast·analysis·채널·사람)으로 되돌릴지 결정. 같은 사유
  (`routing_reason` 등)가 `interaction_protocol.repeat_escalation_threshold`
  회 연속 반복되면 "이 선에서 구조적으로 안 풀림"으로 간주해 `max_rounds`
  소진을 기다리지 않고 상위로 확장
- **연결 구조 — hub-and-spoke, 사슬(chain) 아님**: procurement_plan·
  production_plan·logistics_plan agent 셋 모두와 **개별적으로 직접**
  연결. "조달→생산→배송"은 한 라운드 안에서 의존관계에 따른 **호출 순서**일
  뿐, 구조적 강제가 아님 — 그래서:
  - 배송계획에서 문제 발생 시 생산계획을 거치지 않고 조달계획이나
    supply_coordination으로 바로 되돌아갈 수 있음
  - 이번 요청에 생산 단계가 필요 없으면 건너뛸 수 있음
    (`allocation_candidates[i].required_stages`로 명시)

### procurement_plan agent (Agent, 1개, role_tag: `procurement_plan`)
어느 공급처에서, 언제까지, 얼마에 조달할지 **결정(커밋)**까지가 판단.
판단 전에 **과거 리드타임/신뢰도 패턴 조회(함수)+추정(판단)** 단계가
선행됨 — forecast agent의 데이터 수집·추정 단계와 같은 성격이지만, 공급처 선택과 발주 절차가
명확히 다른 카테고리의 판단이 아니라서 별도 agent로는 안 쪼갬. 실제
자재가 도착하는 물리적 과정은 외부 경계. 공급업체 선정·계약관리는 이미
전제로 주어진 것으로 스코프 밖(발주/주문 처리만 담당). **여러 회사
(forecast agent)의 요청을 공유 자원(공급처 capacity) 문제라 1개 agent
내부에서 안건별로 처리** — forecast agent(회사별 독립 태스크)와 다른
이유: capacity_pools처럼 공유자원을 다루는 agent는 나눌 수 없음
(supply_coordination을 1개로 둔 것과 같은 논리). **데이터 소스**: 실거래
데이터 없음 → 업계 KPI(평균)+표준편차 기반 확률분포 샘플링(추정 입력 +
사후 결과 확인, 양쪽에 재사용, 고정값 아님).

### production_plan agent (Agent, 1개, role_tag: `production_plan`)
생산 일정·라인 배정 **스케줄링 결정**까지가 판단, 과거 리드타임/불량률
패턴 추정 단계 선행(위와 동일한 이유로 별도 agent 분리 안 함). 실제 라인이
도는 것(원자재 투입, 라인 실행)은 외부 경계 — 스케줄링(언제 무엇을
얼마나 생산할지)만 담당하고, 실제 생산/조립 실행·품질테스트·인바운드/
창고운영은 **제외 확정**(물리적 실행 영역). 여러 회사 요청을 1개 agent
내부에서 처리(생산라인이라는 공유자원). **데이터 소스**: 업계 KPI(평균)+
표준편차 기반 확률분포 샘플링(추정+결과 확인 겸용, 고정값 아님).

### logistics_plan agent (Agent, 1개, role_tag: `logistics_plan`)
배송 자원·일정 **배정 결정**까지가 판단, 과거 배송 지연 패턴 추정 단계
선행. 실제 트럭이 움직이는 것은 외부 경계 — 배송 자원·일정 배정만 담당,
실제 운송/배송 실행은 스코프 밖(외부 경계). 여러 회사 요청을 1개 agent
내부에서 처리(배송capacity라는 공유자원). **데이터 소스: SynDelay**(실
데이터로 학습된 생성모델의 합성 데이터, 추정 입력 + 사후 결과 확인 겸용).

## 검증agent (워커풀 방식으로 일감을 받되, 판정만 하고 라우팅은 안 함)

### validation agent(들) (Agent, 도메인별로 분리 — role_tag: `forecast_validation`/
`procurement_validation` 등)
- **일감을 받는 방식은 워커풀**(트리거될 때만 반응, 무기억 — 매번 State를
  새로 읽고 판단, 자기만의 작업 히스토리를 안 가짐. 워커가 몇 명이든
  아무나 집어가도 문제없음) — 이 점은 지속 태스크가 아님
- **판정만 하고 라우팅 권한은 갖지 않는다** — "문제가 생겼을 때 그걸
  어디로 되돌릴지"는 이미 각 agent 자신의 기존 역할(forecast의 핸드오프
  되돌림, supply_coordination의 "공급망계획agent 문제 신호 처리" 등)에
  있는 권한이라 중복되면 안 됨. 이전에 "critical path를 막는 게이트,
  검증agent가 직접 다음 큐에 push"로 정정한 적이 있으나 번복 — 판정과
  라우팅을 분리했다. 상세 흐름은 GRAPH_FLOW.md "검증agent" 참고
- **판단 규칙은 대상 agent의 계산을 재현하지 않고, 독립적 제약조건만
  확인** — 계산을 재현하면 같은 입력에 항상 같은(통과) 결과만 나오는 죽은
  검증이 됨. 예: 배분 합계가 `capacity_pools` 총량을 넘는가, `role_tag`가
  실제로 그 필드 쓰기 권한이 있는가 등 — ①규칙(빠른 함수)으로 처리되고,
  "이 근거가 지금 상황에서 타당한가" 같은 애매한 판단만 ②agent판단(LLM)으로
  감. 실제 비율(얼마나 자주 ②로 가는지)은 구체적인 검증 규칙 목록이
  갖춰져야 알 수 있어 지금은 단정하지 않는다.
- `validation.status`: `"passed"` | `"flagged"` | `"check_failed"` — 검증
  대상 record에 직접 씀(예: `forecast_agents[i].validation`), 현재값만
  유지·이력은 negotiation_log. `passed`는 받는 agent 성격에 따라 갈림 —
  워커풀 성격(`procurement_plan` 등, 안건 단위로 처리)이면 push 없음
  (스스로 pull), 조율 성격(`supply_coordination`)이면 push. `flagged`는
  작성agent의 `validation_result.{role_tag}` 채널로 push, `check_failed`는
  검증agent가 자체 재시도 후 그래도 안 되면 escalation 큐로 push(상세는
  GRAPH_FLOW.md "검증agent" 참고)
- validation agent 자신의 오판(`passed`를 잘못 낸 경우)은 구조적 한계
  (STATE_SCHEMA.md 참고) — 실시간 방지 불가, 다운스트림 불일치로 사후 발견 시
  escalation

## 외부 경계 (그래프 노드 아님, 판단 없음)

물리적으로 실제 일이 일어나는 곳 — 우리가 판단하지 않고, 결과(가능/불가,
실제 수치)만 데이터로 받음. 지금은 파일(실데이터/현실적 시뮬레이션)로
읽되, 하나의 인터페이스 함수로 추상화해서 나중에 실제 API로 교체해도
협상 로직은 안 건드리게 함.

### sales_channel (role_tag: `sales_channel`)
디지털영업(3.5.8) 등 — 입력(forecast agent의 데이터 수집 단계에 과거
실적 제공)과 출력(supply_coordination의 배분 결정을 받아 그 안에서 거래)
둘 다.

### 물리적 조달·생산·배송 실행
각 계획agent의 결정이 실제로 이행됐는지(리드타임, 불량률, 배송지연)의
결과값만 `exchanges[i].response_status`/`response`로 돌아옴 — Step1의
Sensor/Actuator와 같은 성격.

## 사람

### human_manager (사람, Agent 아님, role_tag: `human_manager`)
지속 태스크가 아니라 이벤트 워커풀과 같은 성격 — escalation_records에
새 항목이 생기면 그때 반응(대시보드/알림). escalation_records를 통해
개입 시점과 사유가 기록됨.

## 아직 확정 안 된 것

- supply_coordination agent의 "우선순위 점수 산출"이 순수 함수인지 일부
  판단이 섞이는지 — 실제 구현하면서 재확인
