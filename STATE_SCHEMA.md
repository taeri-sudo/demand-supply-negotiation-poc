# State 스키마

State의 8개 최상위 필드 정의. 구현 중 실제로 이 스키마 자체가 바뀌면
(필드 추가/제거/형태 변경) 이 파일을 직접 고친다 — 바뀐 이유는
JOURNAL.md에 남긴다. 설명은 한글, 실제 필드명/태그값(스키마 코드블록
안)은 영어(snake_case).

## 전체 구조 요약

```
analysis agent(회사별 1개, 총 N개, role_tag: analysis) — 지속 태스크
        ↕  (신호 기반, 핸드오프형)
forecast agent(회사별 1개, 총 N개, role_tag: forecast) — 지속 태스크
        ↕  (신호 기반, 라운드 누적형)
supply_coordination agent(1개, role_tag: supply_coordination) — 지속 태스크
   ↕procurement_plan   ↕production_plan   ↕logistics_plan  (각 1개, 지속 태스크)
   (hub-and-spoke — 셋 다 직접 연결, 사슬 아님. 순서는 의존관계에 따른
    호출 순서일 뿐, 건너뛰기/역방향 되돌림 모두 구조적으로 가능)
        ↓
실제 조달/생산/배송 (그래프 노드 아님, 외부 경계 — sales_channel과 같은 성격)

validation agent(들) — 일감은 이벤트 트리거·무기억 워커풀 방식으로 받지만,
결과는 critical path를 막는 **게이트**(값이 다음 소비자에게 가기 전 항상
거침, 상세는 GRAPH_FLOW.md "검증 게이트" 참고). human_manager(들) —
escalation 발생 시에만 반응, 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
analysis/채널/사람 escalation — 어디까지 되돌릴지는 interaction_protocol이
규정.
```

**동시성 모델**: asyncio 단일 프로세스(Docker/Redis 없이 시작) — 각 지속
태스크가 서로 안 막히고 독립적으로 진행, in-process 신호(asyncio.Queue)로
소통. "라운드"는 그래프 스텝이 아니라 신호 교환으로 구현되며, 모든 분기가
끝나야 다음으로 넘어가는 제약(Pregel 방식의 한계) 자체가 없음. 상세는
GRAPH_FLOW.md·AGENT_NODE_LIST.md 참고.

## 최상위 State 필드

### 1. `analysis_agents[]`
개별 analysis agent 인스턴스(회사별 1개, 총 N개 — asyncio 지속 태스크로
독립 실행).
```
{ agent_id, role_tag: "analysis",
  data_source_basis: "own_company" | "similar_companies",
  model_selection,
  candidates: [{scenario, value, confidence, cost_estimate}],
  validation: { status: "passed" | "flagged" | "check_failed", suspected_cause, rationale, ts, validator_role_tag }
}
```
후보(a/b/c) 선택은 forecast agent 쪽에서 판단3계층 적용 — 신뢰구간이
좁으면 규칙(①)으로 자동 채택, 비용-리스크 트레이드오프가 얽히면
agent판단(②), 통계와 비즈니스 판단이 충돌하면 사람 escalation(③).
`data_source_basis`는 자사 과거 실적이 없거나 부적합할 때(신제품·신규
프로모션 등) 유사 업종/유사 사례(외부 데이터)로 대체할지를 analysis agent가
판단한 결과 — "회사 단위 인스턴스"가 곧 "그 회사 데이터만 참고"를 뜻하지
않음.

### 2. `forecast_agents[]`
개별 forecast agent 인스턴스(회사별 1개, 총 N개, 같은 agent_id로
analysis_agents와 짝). analysis와는 **핸드오프형**(같은 값을 다듬는 게
아니라 재실행 지시) 관계 — 자세한 상호작용 유형은 GRAPH_FLOW.md 참고.
```
{ agent_id, role_tag: "forecast", selected,
  selection_basis: "rule" | "agent_judgment" | "human",
  current_round, round_history: [{round, proposed, response}],
  validation: { status: "passed" | "flagged" | "check_failed", suspected_cause, rationale, ts, validator_role_tag }
}
```
`validation`은 **현재값만** 유지(이력
전체는 아래 `negotiation_log`에 쌓임 — Step1 원칙3: 판단용 현재값과 기록용
스냅샷 분리).

### 3. `capacity_pools[]`
생산capacity를 "계좌"처럼 관리. 공유풀/전용풀 둘 다 표현 가능.
```
{ pool_id, total_capacity, remaining_capacity,  # 증감 가능
  adjustment_history: [{ts, delta, reason}],
  linked_role_tags: ["forecast"]  # 인스턴스 나열이 아니라 역할 태그로 연결
}
```

### 4. `allocation_candidates[]` (현재 라운드)
supply_coordination agent가 만드는 후보 배분안. "M개 agent"가 아니라 "1개
agent가 만드는 M개 후보"로 처리. 우선순위는 4단 구조로 산출(아래 "우선순위
구조" 참고).

선정된 안(`status: "selected"`)에는 공급망계획agent(procurement_plan/
production_plan/logistics_plan)와의 집행 교환 내역을 내장한다 — 별도 배열을
만들지 않음. "공급망 조율"의 일부이지 별개 데이터가 아니라는 게 이유이고,
`role_tag`로 어느 공급망계획agent 몫인지 구분하므로 공급망계획agent 종류가 늘어도 새
배열이 필요 없다. 각 교환은 **배열**(`exchanges[]`)로 둬 옵션A(단발
질의응답)에서 옵션B(다회 협상)로 확장돼도 스키마 변경이 없게 하고, 개별
타임스탬프(`requested_at`/`responded_at`)로 공급망계획agent마다 다른 시작·종료
시점을 표현한다(트랜잭션처럼 서로 겹치거나 어긋날 수 있음). 라운드 상한은
이 edge에도 `interaction_protocol`의 `max_rounds`를 그대로 적용 — 새 규칙
불필요.

`required_stages`로 이번 배분안에 실제로 필요한 공급망계획agent만 명시 —
supply_coordination agent가 procurement_plan·production_plan·logistics_plan
셋 모두와 직접 연결(hub-and-spoke)되어 있어, 순서를 따르되(의존관계상
조달→생산→배송 순으로 호출) 특정 단계를 건너뛰거나(예: 생산 불필요),
문제 발생 시 중간 단계를 거치지 않고 바로 이전 단계나 supply_coordination
으로 되돌아갈 수 있음(사슬처럼 앞뒤로만 이동하는 구조가 아님).
```
{ plan_id, allocation: {forecast_agent_id: quantity}, cost, risk,
  status: "generated" | "selected" | "rejected",
  required_stages: ["procurement_plan", "production_plan", "logistics_plan"],
  exchanges: [
    { role_tag: "procurement_plan", round: 1, request: {...},
      response: {...}, response_status: "feasible" | "infeasible" | "in_progress",
      requested_at, responded_at, handled_by,  # 워커풀이면 누가 처리했는지(선택)
      routing_reason,  # infeasible일 때 supply_coordination이 어디로/왜 되돌렸는지
      validation: { status: "passed" | "flagged" | "check_failed", suspected_cause, rationale, ts, validator_role_tag } },
    { role_tag: "production_plan", round: 1, ... }
  ]
}
```

## 우선순위 구조 (자원 배분 시 사용)

"몇 번까지 기다릴지"(max_rounds, 안전장치)와 "누구부터/얼마나 배분할지"
(우선순위)는 다른 문제 — 전자는 규칙(①) 최후 방어선이고, 후자는 **동점
처리(tie-breaker) 순서**로 산출(4단 모두가 매번 적용되는 게 아니라, 앞
기준에서 확실한 차이가 안 날 때만 다음 기준으로 내려감):

1. **매출/마진 임팩트** (주 기준) — 대부분 이 값만으로 우열이 갈림. 이
   프로젝트의 목적(이익 연결)과 직결
2. **예측 신뢰도 보정** — 1번이 임계치 이내로 비슷할 때만 적용, 과거 예측
   정확도 이력으로 순위 조정(자주 틀린 채널은 하향)
3. **Aging 보정(하한선)** — 1·2번도 비슷할 때만 적용, 대기시간에 비례해
   보정. "정하는 기준"이 아니라 "굶주림 방지 하한선"
4. **배분량(capacity)** — 축이 아니라 위 순서로 정렬한 뒤 결정되는 산출물

```
priority_queue_entry = { agent_id, wait_start_ts, revenue_impact,
  forecast_reliability, aging_adjustment, priority_score }
```

### forecast_reliability 산출과 저장

예측치와 실제값을 비교할 때 매번 같은 결과만 나오지 않도록 **walk-forward
validation**(과거 데이터를 시간순으로 잘라, 그 시점까지의 데이터로 다음
구간을 예측 → 실제값과 비교 → 기준 시점을 한 칸씩 밀며 반복하는 표준 검증
방식 — 실제 운영 중인 예측 시스템이 매 계획주기마다 겪는 상황을 과거
데이터로 재현하는 것과 같음)으로 계산. 시뮬레이션에서는 압축된 시계로
재생(streaming) — 데이터의 시간단위(일/주/월)는 그대로 두고 재생 속도만
빠르게.

계산된 점수는 한 번의 실행이 끝나도 다음 계획주기 실행 때 남아있어야 하므로
**경량 저장소(SQLite 등)에 영속화**. 이건 LLM을 파인튜닝하는 게 아님 — LLM
가중치는 그대로 두고, LLM 밖의 참고자료(숫자)만 남겼다가 다음 판단 시
제공하는 것.

## 동시성 모델 (asyncio)

- **구조**: analysis/forecast(회사별, 총 N개씩)와 supply_coordination/
  공급망계획agent(각 1개)를 각각 독립된 **asyncio 태스크**로 실행 — Docker/
  Redis 같은 별도 프로세스·네트워크 없이, 파이썬 프로세스 하나 안에서
  이벤트루프가 태스크들을 오가며 진행(cooperative multitasking)
- **왜 Send API(LangGraph)가 아닌지**: Send API는 "한 스텝 안에서 N개로
  갈라졌다가 그 스텝이 끝나야 합쳐지는" 모델이라, "A회사 처리 중 응답을
  기다리는 동안 B회사 작업을 진행"하는 진짜 독립적 병행이 안 됨. asyncio
  태스크는 이게 가능 — `asyncio.create_task()`로 태스크를 만들고, I/O
  대기(LLM 응답 대기 등) 중엔 다른 태스크가 자동으로 진행됨
- **통신**: agent 간 직접 호출이 아니라 항상 State(공유 데이터)를 거침 —
  한쪽이 State에 값을 쓰고 in-process 신호(`asyncio.Queue`)로 알리면,
  상대가 그 신호를 받아 State를 읽고 반응. Redis 같은 외부 큐가 아니라
  **같은 프로세스 안의 신호**일 뿐(기록은 여전히 State/negotiation_log가 담당,
  큐는 "초인종" 역할만). **값 쓰기와 push는 항상 짝** — `set_field`가 값
  기록과 동시에 큐에 push하도록 구현(업무 로직이 매번 기억할 필요 없게)
- **검증 게이트**: 값을 쓴 agent는 원래 의도한 다음 agent 큐가 아니라
  **검증agent 큐에만 push** — 검증 통과 후에야 검증agent가 원래 목적지로
  push(상세는 GRAPH_FLOW.md "검증 게이트" 참고). 그래서 각 지속 태스크는
  기존 협상 채널 외에 자기 `validation_result.{role_tag}` 채널도 지켜봐야 함
- **State 접근 통제**: 모든 State 읽기/쓰기는 `role_permissions`를 검사하는
  wrapper 함수(예: `get_field(role_tag, field_path)`)를 통해서만 — 이렇게
  강제해야 코드가 몰래 다른 role의 영역을 직접 건드리는 걸 런타임에 막을 수 있음
- **공유 자원 보호**: `capacity_pools`처럼 여러 태스크가 동시에 건드릴 수
  있는 값은 `asyncio.Lock`으로 감싸 동시 수정(race condition) 방지
- **확장 여지(지금 안 만듦)**: 위 원칙(State를 거쳐서만 통신, wrapper로
  접근 통제)을 지키면, 나중에 in-process 큐를 Redis 등 외부 큐로 교체해
  물리적으로 분리된 서버와 통신하게 확장 가능 — agent 로직은 안 건드리고
  큐 구현체만 교체

### 5. `negotiation_log[]`
전체 조정 과정을 취합하는 로그. `exchanges`가 "한 역할과의 요청-응답 쌍"을
담는 개별 트랜잭션이라면, 이건 조달·생산·배송처럼 **서로 다른 역할들의
이벤트가 뒤섞여 시간순으로 쌓이는 전체 스트림**(DB의 트랜잭션 로그와 같은
역할) — `role_tag`로 필터링 없이 그대로 읽으면 여러 역할의 시작/종료가
어떻게 겹치거나 어긋나는지 파악 가능. Step1 원칙3(판단용 현재값 vs 기록용
스냅샷 분리) 재적용: `exchanges`=판단에 쓰는 현재 상태, `negotiation_log`=
기록(감사·역추적용). (LangSmith와 별개 — 이건 agent 판단이 실제로 참조하는
데이터, LangSmith는 사람이 보는 디버깅용)

검증 이벤트도 별도 필드 없이 여기에 함께 쌓임 — "통과"/"이상감지" 각각의
현재값은 해당 record(`analysis_agents[i].validation` 등)에 있고, 그
이력(언제 통과였다가 언제 번복됐는지)은 이 로그를 시간순으로 읽으면 됨.
```
{ role_tag, event, round, ts }
```

### 6. `interaction_protocol[]`
agent 역할 간 상호작용 규칙(동역학). `scope`는 인스턴스 나열이 아니라 역할
태그.
```
{ edge: "forecast<->supply_coordination", max_rounds: 3,  # 타임아웃 안전장치
  repeat_escalation_threshold: 2,  # 같은 사유(routing_reason 등)가 이 횟수만큼
                                    # 연속 반복되면 max_rounds 소진을 안 기다리고
                                    # "구조적으로 안 풀림"으로 간주해 상위로 확장
  scope: ["forecast", "supply_coordination"],
  escalation_trigger, escalation_target, escalation_kind: "rule" | "agent_judgment" | "human",
  source: "initial_design" | "promoted_from_trace" | "external_benchmark",
  last_updated
}
```

**갱신 경로 (파인튜닝 아님 — LLM 가중치는 그대로, 이 참고자료만 바뀜)**:
- `promoted_from_trace`(내부): `negotiation_log`(이벤트 로그)를 process
  mining 라이브러리(pm4py 등)로 분석해 패턴(예: "이 edge는 보통 N라운드
  안에 수렴")을 발견 → `max_rounds` 등 값 조정
- `external_benchmark`(외부): 업계 벤치마크 자료(유사 협상/거래의 평균
  소요 등)를 초기값/조정 근거로 사용
- 둘 다 규칙(①) 계층의 기준값만 바꿈 — agent의 판단 능력 자체가 좋아지는
  게 아니라, 판단에 쓰는 참고자료가 갱신되는 것

### 7. `role_permissions[]`
State 필드 단위 접근권한(agent 간, Unity Catalog의 시스템 접근통제와는 다른
층). r/w만 사용(x는 불필요), 화이트리스트 방식 — "전체 접근"은 기본값이
아니라 예외적으로만 명시.
```
{ role_tag, field_path, access: "r" | "w" }
```
예: procurement_plan agent는 `capacity_pools`를 직접 못 읽고,
`allocation_candidates[selected].exchanges`에서 자기 `role_tag`에 해당하는 항목만 r/w.

### 8. `escalation_records[]`
사람(human_manager) 개입 기록.
```
{ trigger_edge, reason, target_role: "human_manager", status, resolution }
```

## 구조적 한계

validation agent 자신의 오판(예: 실제로는 문제인데 "통과"로 잘못 판정)은
실시간 방지가 불가능함 — 검증이 게이트(관문)가 되어 다음 단계 진행을
막아도, 그건 "이상감지로 판정된 경우"에만 막는 것이지 "잘못 통과시킨
경우"까지는 못 잡음. "검증을 검증하는" 상위 검증을 또 두면 같은 문제가
무한히 반복되므로(Step1이 정직하게 남긴 한계 목록과 같은 성격). 대신
다운스트림에서 실제 값이 그 통과 기록과 어긋나야(예: 다음 라운드 실적이나
실제 배송 결과와 크게 벗어남) 사후적으로만 발견되고, 발견 즉시 자동
재처리하지 않고 `escalation_records`로 사람에게 감(이 시점엔 어느
validation agent가 왜 틀렸는지 자체를 신뢰할 수 없는 상태이므로).

## 데이터 소스 (mock 대신)

- **analysis agent 입력**: Kaggle Store Item Demand — 실데이터. 자사
  이력이 없거나 부적합하면(`data_source_basis: "similar_companies"`) 유사
  업종/유사 사례 데이터로 보강
- **logistics_plan agent 응답값**(리드타임/지연 패턴): SynDelay — 실데이터로
  학습된 생성모델의 합성 데이터(완전 mock보다 현실적), 추정 입력과 사후
  결과 확인 양쪽에 재사용
- **procurement_plan·production_plan agent 응답값**: 접근 가능한 실거래
  데이터셋을 못 찾음 → 고정값이 아니라, 실제 업계 공개 KPI(평균 리드타임·
  결함률)와 보고된 표준편차를 파라미터로 한 **확률분포 샘플링**(SynDelay와
  같은 원리) — 매번 같은 값만 나오는 걸 방지. 추정 입력과 사후 결과 확인
  양쪽에 재사용
- 위 모든 데이터는 **하나의 인터페이스 함수로 추상화해서** 읽음 — 나중에
  실제 회사 데이터/API로 교체할 때 이 함수 내부만 바꾸면 되고, 협상 로직
  (agent 판단)은 안 건드림

## 아직 정하지 않은 것

- 공급망계획agent 간(procurement_plan↔production_plan 등) 상호작용까지 다회
  협상으로 갈지 — 지금은 supply_coordination agent를 경유하는 것으로
  가정, 구조상 확장 가능하게만 열어둠
- 그래프 흐름(엣지, self-loop, 종료조건, 인스턴스 패턴 기준)은
  GRAPH_FLOW.md 참고 — 실행층이 실제 agent가 되면 `execution_records[]`
  등 새 최상위 필드로 분리 예정(지금은 외부 경계라 해당 없음)
