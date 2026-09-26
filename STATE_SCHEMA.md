# State 스키마

State의 7개 최상위 필드 정의와 필드 수준의 규칙. 구현 중 실제로 이 스키마
자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일을 직접 고친다 — 바뀐 이유는
JOURNAL.md에 남긴다. 설명은 한글, 실제 필드명/태그값(스키마 코드블록
안)은 영어(snake_case). agent의 역할·내부 단계는 AGENT_NODE_LIST.md,
agent 간 연결·신호·동시성은 GRAPH_FLOW.md 참고.

## 전체 구조 요약

```
forecast agent((회사,item) 인스턴스별 1개, 총 N개, role_tag: forecast) — 지속 태스크
   (시나리오 정의→데이터 수집·소스 판단→예측기법 선택→시나리오별 예측 계산→
    발생 가능성 평가→시나리오 선택을 한 agent 내부 단계로 수행)
        ↕  (신호 기반, 평소 정방향 최적화 / 예외 시에만 역방향 핸드오프)
supply_coordination agent(1개, role_tag: supply_coordination) — 지속 태스크
   ↕procurement_plan   ↕production_plan   ↕logistics_plan  (각 1개, 지속 태스크)
   (hub-and-spoke — 셋 다 직접 연결, 사슬 아님. 순서는 의존관계에 따른
    호출 순서일 뿐, 건너뛰기/역방향 되돌림 모두 구조적으로 가능)
        ↓
실제 조달/생산/배송 (그래프 노드 아님, 외부 경계 — sales_channel과 같은 성격)

validation agent(들) — 일감은 이벤트 트리거·무기억 워커풀 방식으로 받고,
판정(`passed`/`flagged`/`check_failed`)만 State에 쓴다 — 라우팅 권한은
없다. human_manager(들) — escalation 발생 시 반응(알림은 받기만 함),
지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 되돌릴지는 interaction_protocol이
규정.
```

**동시성 모델**: asyncio 단일 프로세스 — 각 지속 태스크가 서로 안 막히고
독립적으로 진행, in-process 신호(asyncio.Queue)로 소통. 상세는
GRAPH_FLOW.md "동시성 모델" 참고.

## 최상위 State 필드

### 1. `forecast_records[]`
forecast agent 인스턴스((회사, item) 조합별 1개)가 State에 남기는 현재값
기록. 다른 agent가 이 값을 읽는다(`role_permissions`로 통제).
```
{ agent_id, company_id, item_id, pool_key, role_tag: "forecast",
  scenarios: [
    { scenario_id,
      assumptions: [                                        # 비어 있으면 기준 시나리오(현재 추세 유지)
        { driver: "category_trend" | "price" | "event",     # 수요 동인: 무엇이 변한다고 가정하는가
          demand_effect,                                    # 그로 인한 수요 변화율(부호 있음, +0.1 = 10% 증가)
          evidence: { kind, item_scope, refs } }            # 가정의 근거 데이터
      ],
      defined_by: "rule" | "agent_judgment",
      value,                                                # 이 시나리오의 예측값
      forecast_uncertainty,                                 # 이 시나리오 예측의 흔들림
      likelihood,                                           # 이 시나리오가 실제로 일어날 가능성
      cost_estimate }                                       # 이 시나리오대로 준비했는데 다른 시나리오가
                                                            # 실현됐을 때의 손실, 각 likelihood로 가중
  ],
  data_sources: [
    { kind: "orders" | "pos" | "market",                   # 데이터 출처
      item_scope: "same_item" | "similar_item" | "category",  # 예측 대상 item과의 관계
      refs: [...],                                          # 실제로 참고한 item/출처
      use_from: date | null }                               # 이 날짜 이전 데이터는 쓰지 않음
  ],
  excluded_sources: [
    { kind, item_scope, refs, reason: "irrelevant" }        # 관련 없는 데이터 — 재실행 시 다시 고르지 않음
  ],
  cleaning: { applied, count },                             # 표준 정제 적용 기록
  forecast_method,                                          # 통계 예측기법
  selected_scenario,
  selection_basis: "rule" | "agent_judgment" | "human",
  validation: { status: "passed" | "flagged" | "check_failed",
                suspected_cause, rationale, ts, validator_role_tag }
}

suspected_cause: {
  type:   "scenario" | "data_source" | "forecast_method",
  issue:  (type별 하위 사유 — 아래 참고) | null,
  scenario_id: ... | null,                                   # type이 scenario일 때 문제가 난 시나리오
  source: { kind, item_scope } | null,                       # type이 data_source일 때 문제가 난 소스
  use_from: date | null                                      # 시점 기준으로 무관할 때
}
```

인스턴스 단위는 (회사, item) 조합이다 — 한 회사가 여러 item(예: 라면과
과자)을 동시에 주문할 수 있어, 회사 단위로만 나누면 item별로 다른 상태
(하나는 정상, 하나는 데이터 소스 문제)를 표현할 수 없다. `agent_id`는
`"{company_id}:{item_id}"` 같은 합성키로 두되, 파싱 대상이 아니라 표시용
키로만 쓰고, 실제 필터링/조회는 `company_id`/`item_id` 필드로 한다.
`company_id`는 **필수이며 null을 허용하지 않는다** — 고객사가 정해지지 않은 수량(예: 신제품 첫
물량)은 forecast를 거치지 않는 human_input 수요로 들어온다(아래 "아직
정하지 않은 것" 참고).

`pool_key`는 forecast 단계에서 agent가 직접 정하는 값이 아니다 — 어느
생산라인에 배정될지는 production_plan의 판단이므로, `allocation_candidates
[i].exchanges`의 production_plan 응답으로 정해진 뒤 참조용으로 기록만
된다(아래 `capacity_pools` 절 참고).

**시나리오 — 2단 구조.** 시나리오는 서로 다른 가정에서 나온 서로 다른
예측이다. 시나리오가 여러 개 있고, 각 시나리오 안에 가정이 여러 개 있을 수
있다. 가정이 여러 개인 시나리오는 그 가정들이 **동시에 일어나는 하나의
미래**이며, 효과를 합쳐 예측값 하나·비용 하나를 갖는다.
- `driver`(수요 동인)는 데이터가 실제로 존재하는 요인만 값으로 둔다 —
  `category_trend`(카테고리 추세, 시장 데이터), `price`(가격 변경),
  `event`(프로모션·명절 같은 캘린더 이벤트). 자유 텍스트로 두면 근거를
  검증할 수 없다. 새 요인이 필요해지면 값을 추가한다.
- `evidence`는 아래 `data_sources`와 같은 어휘(`kind`, `item_scope`)를 써서
  "근거가 실제로 수집된 데이터에 있는가"를 바로 확인할 수 있게 한다.
- `defined_by`는 시나리오를 규칙이 만들었는지(`"rule"`) agent 판단이
  만들었는지(`"agent_judgment"`)를 기록한다.
- `likelihood`는 한 인스턴스의 시나리오들끼리 합이 1이 되도록 맞춘다.
- `cost_estimate`의 단위당 과잉 비용·부족 비용은 공개 데이터에 없어
  카테고리별 가정값으로 두고, 가정임을 DESIGN.md에 명시한다.

시나리오 검증 조건(대상 agent의 계산을 재현하지 않는 독립 제약조건):
- `evidence`가 이번 주기 스냅샷 안에 실제로 존재하는가
- `demand_effect`가 과거 변동 범위 안에 있는가
- 시나리오끼리 `value` 차이가 최소 기준 이상인가(사실상 같은 예측 방지)
- 한 시나리오 안의 두 가정이 같은 근거를 중복 사용하지 않는가(예:
  가격 인하가 포함된 프로모션을 `price`와 `event`로 이중 계산)

**데이터 소스 — `kind`와 `item_scope`는 성격이 다른 두 변수다.**

| 변수 | 값 | 정의 |
|---|---|---|
| `kind` (데이터 출처) | `orders` | 고객사(`company_id`)가 우리에게 한 주문 |
| | `pos` | 고객사(`company_id`) 매장 판매 중 우리 제품만 |
| | `market` | 경쟁사를 포함한 전체 시장 데이터 |
| `item_scope` (예측 대상 item과의 관계) | `same_item` | 예측 대상 item(`item_id`) |
| | `similar_item` | 예측 대상 item(`item_id`)과 비슷한 제품 |
| | `category` | 예측 대상 item(`item_id`)이 속한 제품군 |

두 변수는 독립적이고, 정의상 9가지 조합이 모두 성립한다. 조합의 의미는
두 정의를 이어 붙이면 된다(예: `orders` + `similar_item` = 고객사가 우리에게
한 주문 중, 예측 대상 item과 비슷한 제품의 주문). 누구의 제품인지는 출처에서
따라 나온다 — `orders`/`pos`는 우리 제품만, `market`은 우리 제품과 경쟁사
제품. 현재 데이터로 채울 수 있는 조합은 AGENT_NODE_LIST.md forecast agent
"입력" 참고.

**되돌림 사유(`suspected_cause`)** — `type`은 문제가 난 대상, `issue`는
그 대상의 하위 사유:

| type | issue |
|---|---|
| `scenario` | `no_evidence`(근거가 스냅샷에 없음) / `effect_out_of_range`(효과가 과거 변동 범위를 벗어남) / `not_distinct`(시나리오 간 차이 없음) / `double_counted`(가정 간 근거 중복) — 위 시나리오 검증 조건과 1:1 대응 |
| `data_source` | `insufficient`(부족) / `contaminated`(오염) / `irrelevant`(무관 — `use_from`이 있으면 시점 기준, 없으면 관련 없는 소스) |
| `forecast_method` | 없음(직전 예측기법이 문제) |

사유별 재개 지점과 대응은 AGENT_NODE_LIST.md forecast agent "되돌림" 참고.
검증agent의 `flagged`와 supply_coordination→forecast 역방향 되돌림은 같은
형식으로 보낸다.

`validation`은 **현재값만** 유지(이력 전체는 `negotiation_log`에 쌓임 —
판단용 현재값과 기록용 스냅샷 분리).

### 2. `capacity_pools[]`
생산capacity를 "계좌"처럼 관리. 공유풀/전용풀 둘 다 표현 가능.
```
{ pool_id, total_capacity, remaining_capacity,  # 증감 가능
  adjustment_history: [{ts, delta, reason}],
  linked_pool_key: "면류_라인A"  # 인스턴스 나열이 아니라 자원 공유 단위(태그)로 연결
}
```

`linked_pool_key`는 forecast 인스턴스 쪽의 `pool_key` 필드(위
`forecast_records[]` 참고)와 같은 값을 가진 인스턴스를 연결한다 — 같은
값을 가진 인스턴스가 여럿이면 그 풀은 공유풀, 하나뿐이면 전용풀이 된다
(풀 종류를 별도 표시하지 않고 연결 개수의 결과로만 드러남). `pool_key`는
도메인 카테고리(예: "면류")가 아니라 **실제로 같은 생산라인/설비를
공유하는가** 기준으로 묶는다 — 같은 카테고리여도 라인이 다르면 다른
`pool_key`.

**capacity_pools의 실제 접근 주체는 supply_coordination과
production_plan뿐이다.** procurement_plan·logistics_plan은 `capacity_pools`를
쓰지 않는다 — 이 둘의 제약은 "여러 요청이 실시간으로 같은 잔여량을
나눠 갖는" 계좌형 공유 자원이 아니라, 요청마다 확률분포로 독립적으로
feasible/infeasible을 응답하는 구조이기 때문(AGENT_NODE_LIST.md 각 agent의
데이터 소스 참고). logistics_plan은 아직 agent 자체가 미구현이라(M5) 권한
설정도 그때 정해진다.

### 3. `allocation_candidates[]` (현재 라운드)
supply_coordination agent가 만드는 후보 배분안. "M개 agent"가 아니라 "1개
agent가 만드는 M개 후보"로 처리. 우선순위 산출 규칙은 AGENT_NODE_LIST.md
supply_coordination agent 참고.

candidate 자체(`allocation`/`cost`/`risk` 등)의 생성은 forecast의 선택값을
받아 우선순위 점수를 산출하는 **단방향 최적화 결과**이지 협상이 아니다 —
라운드가 쌓이는 건 아래 `exchanges[]`(supply_coordination↔공급망계획agent
간 라운드)뿐이며, `infeasible` 응답이 왔을 때만 라운드가 이어진다.
`feasible`/`infeasible` 비율은 procurement_plan 등의 확률분포 파라미터
(평균/표준편차)에 따라 달라지며 지금은 확정하지 않는다.

선정된 안(`status: "selected"`)에는 공급망계획agent(procurement_plan/
production_plan/logistics_plan)와의 집행 교환 내역을 내장한다 — 별도 배열을
만들지 않음. "공급망 조율"의 일부이지 별개 데이터가 아니라는 게 이유이고,
`role_tag`로 어느 공급망계획agent 몫인지 구분하므로 공급망계획agent 종류가 늘어도 새
배열이 필요 없다. 각 교환은 **배열**(`exchanges[]`)로 둬 단발 질의응답에서
다회 협상으로 확장돼도 스키마 변경이 없게 하고, 개별 타임스탬프
(`requested_at`/`responded_at`)로 공급망계획agent마다 다른 시작·종료 시점을
표현한다(서로 겹치거나 어긋날 수 있음). 라운드 상한은 이 edge에도
`interaction_protocol`의 `max_rounds`를 그대로 적용.

`required_stages`는 이번 배분안에 실제로 필요한 공급망계획agent만 명시한다
— 여기 없는 agent는 호출되지 않는다(연결 구조는 GRAPH_FLOW.md
"공급망계획agent 연결 구조" 참고).
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

`response_status: "infeasible"`은 "요청대로는 아예 불가능하다"만 뜻하지
않는다 — procurement_plan 등이 공급처·시점·조달량을 판단하는 과정에서
자연히 나오는 "요청과 다르지만 실제로 더 정확하거나 나은 대안이 있다"는
판단도 infeasible로 분류한다. 이건 원래 그 agent가 하는 판단(어디서/
언제/얼마나)의 부산물이지, infeasible 여부를 가르기 위한 별도 계산
단계가 아니다.

우선순위 대기열 항목(산출 규칙은 AGENT_NODE_LIST.md supply_coordination
agent 참고):
```
priority_queue_entry = { agent_id, wait_start_ts, revenue_impact,
  forecast_reliability, aging_adjustment, priority_score }
```

### 4. `negotiation_log[]`
전체 조정 과정을 취합하는 로그. `exchanges`가 "한 역할과의 요청-응답 쌍"을
담는 개별 트랜잭션이라면, 이건 조달·생산·배송처럼 **서로 다른 역할들의
이벤트가 뒤섞여 시간순으로 쌓이는 전체 스트림**(DB의 트랜잭션 로그와 같은
역할) — `role_tag`로 필터링 없이 그대로 읽으면 여러 역할의 시작/종료가
어떻게 겹치거나 어긋나는지 파악 가능. Step1 원칙3(판단용 현재값 vs 기록용
스냅샷 분리) 재적용: `exchanges`=판단에 쓰는 현재 상태, `negotiation_log`=
기록(감사·역추적용). (LangSmith와 별개 — 이건 agent 판단이 실제로 참조하는
데이터, LangSmith는 사람이 보는 디버깅용)

검증 이벤트도 별도 필드 없이 여기에 함께 쌓임 — "통과"/"이상감지" 각각의
현재값은 해당 record(`forecast_records[i].validation` 등)에 있고, 그
이력(언제 통과였다가 언제 번복됐는지)은 이 로그를 시간순으로 읽으면 됨.
```
{ role_tag, event, round, ts }
```

### 5. `interaction_protocol[]`
agent 역할 간 상호작용 규칙(동역학). `scope`는 인스턴스 나열이 아니라 역할
태그.
```
{ edge: "supply_coordination<->procurement_plan", max_rounds: 3,  # 타임아웃 안전장치
  repeat_escalation_threshold: 2,  # 같은 사유(routing_reason 등)가 이 횟수만큼
                                    # 연속 반복되면 max_rounds 소진을 안 기다리고
                                    # "구조적으로 안 풀림"으로 간주해 상위로 확장
  scope: ["supply_coordination", "procurement_plan"],
  escalation_trigger, escalation_target, escalation_kind: "rule" | "agent_judgment" | "human",
  escalation_mode: "intervention" | "notice",  # 멈추고 사람의 결정을 기다림 / 알리고 진행
  source: "initial_design" | "promoted_from_trace" | "external_benchmark",
  last_updated
}
```

**forecast<->supply_coordination는 방향에 따라 성격이 다르지만**(정방향은
단방향 전달=최적화, 역방향은 공급망계획agent의 infeasible 신호로 여는
핸드오프=재실행 지시 — GRAPH_FLOW.md 엣지 표 참고), **두 방향 모두
라운드가 쌓이지 않아 `max_rounds`가 필요 없다** — 그래서 정방향/역방향을
나누지 않고 레코드 하나를 함께 쓴다:
```
{ edge: "forecast<->supply_coordination", repeat_escalation_threshold: 2,
  scope: ["forecast", "supply_coordination"],
  escalation_trigger, escalation_target, escalation_kind,
  source: "initial_design", last_updated }
  # max_rounds 없음: 정방향(최적화)·역방향(핸드오프) 모두 라운드 개념이
  # 없다. repeat_escalation_threshold/escalation_*는 검증agent의 flagged
  # 되돌림 경로(GRAPH_FLOW.md "검증agent" 참고)가 두 방향 모두에 동일하게
  # 적용되므로 유지한다
```
다른 엣지(supply_coordination↔procurement_plan 등)는 요청→응답→
(infeasible 시) 라운드 연장이 실제로 있어 위 첫 예시처럼 `max_rounds`를
쓴다. `InteractionProtocol` pydantic 모델에서 `max_rounds`를 필수에서
선택(Optional)으로 바꿀지는 이 구분을 실제로 구현하는 마일스톤에서
정한다.

**forecast → human_manager**(최소 구매 약정과 예측의 차이, AGENT_NODE_LIST.md
forecast agent 참고)는 supply_coordination과 무관한 별도 엣지다. 진행을 멈추지
않는 `notice` 모드라 라운드가 없고, 알림 기준값만 둔다. 약정의 구속력에 따라
기준이 다르다(구속력이 없으면 우리가 손실을 떠안으므로 더 작은 차이에도 알림):
```
{ edge: "forecast->human_manager", scope: ["forecast", "human_manager"],
  escalation_trigger: "commitment_gap", escalation_target: "human_manager",
  escalation_kind: "rule", escalation_mode: "notice",
  notice_threshold: { binding: 0.2, non_binding: 0.05 },  # 예측과 약정 잔여량의 차이 비율
  source: "initial_design", last_updated }
```

**갱신 경로**:
- `promoted_from_trace`(내부): `negotiation_log`(이벤트 로그)를 process
  mining 라이브러리(pm4py 등)로 분석해 패턴(예: "이 edge는 보통 N라운드
  안에 수렴")을 발견 → `max_rounds` 등 값 조정
- `external_benchmark`(외부): 업계 벤치마크 자료(유사 협상/거래의 평균
  소요 등)를 초기값/조정 근거로 사용
- 둘 다 규칙(①) 계층의 기준값(`max_rounds`, `notice_threshold` 등)만 바꿈

### 6. `role_permissions[]`
State 필드 단위 접근권한(agent 간, Unity Catalog의 시스템 접근통제와는 다른
층). r/w만 사용(x는 불필요), 화이트리스트 방식 — "전체 접근"은 기본값이
아니라 예외적으로만 명시.
```
{ role_tag, field_path, access: "r" | "w" }
```
예: procurement_plan·logistics_plan agent는 `capacity_pools`를 직접 못
읽고(이 두 agent의 제약은 계좌형 공유 자원이 아니라 확률분포 응답이라
애초에 참조 대상이 아님), `allocation_candidates[selected].exchanges`
에서 자기 `role_tag`에 해당하는 항목만 r/w. `capacity_pools`는
`supply_coordination`(r, 배분 판단용)과 `production_plan`(r/w, 자기
라인 자원이므로)만 접근한다.

### 7. `escalation_records[]`
사람(human_manager)에게 올라간 건의 기록. `mode`로 종류를 구분한다 —
`intervention`은 진행을 멈추고 사람의 결정을 기다리는 건, `notice`는 알리기만
하고 진행하는 건(사람의 결정이 없으므로 `resolution`이 없음).
```
{ trigger_edge, reason, target_role: "human_manager",
  mode: "intervention" | "notice",
  status, resolution }   # resolution은 intervention일 때만
```

## State 접근 규칙

- 모든 State 읽기/쓰기는 `role_permissions`를 검사하는 wrapper 함수(예:
  `get_field(role_tag, field_path)`, `set_field`)를 통해서만 한다 — 코드가
  다른 role의 영역을 직접 건드리는 걸 런타임에 막기 위함. `set_field`는 값
  기록과 동시에 해당 큐에 push한다(GRAPH_FLOW.md "Push/Pull 용어 정리" 참고).
- `capacity_pools`처럼 여러 태스크가 동시에 건드릴 수 있는 값은
  `asyncio.Lock`으로 감싸 동시 수정(race condition)을 막는다.

## 아직 정하지 않은 것

- 실행층이 실제 agent가 되면 `execution_records[]` 등 새 최상위 필드로 분리
  예정(GRAPH_FLOW.md 참고, 지금은 외부 경계라 해당 없음)
- **forecast를 거치지 않는 수요(human_input)의 State 반영 구조 (M4)**:
  `allocation_candidates.allocation`(`{forecast_agent_id: quantity}`)이
  forecast 인스턴스에 묶여 있어, forecast 없이 들어오는 수량을 표현하지
  못한다. 대상은 새 고객사 첫 주문, 신제품 첫 물량, 프로모션 이력이 없는
  고객사·item의 프로모션 수량. 고객사가 정해지지 않은 수량(신제품 첫 물량)도
  있으므로 이 구조에서는 고객사 지정이 선택이다. `demand_id`/`source` 등으로
  확장 필요 — `forecast_records` 스키마와는 별개 변경.
- **forecast agent 세부(M2 진행 중 확정 예정)**: 시장 데이터 출처와 카테고리
  매핑, POS 데이터셋 확정, `cost_estimate`의 카테고리별 단위 비용 가정값
- **공급 보장 물량 미충족 시 escalation과 sales_channel "조정 불가" 통지의
  순서**: escalation이 먼저 사람에게 조치 기회를 준 뒤에만 sales_channel로
  "조정 불가"가 나가야 함(순서가 반대면 사람이 풀 수 있었던 건이 이미
  불가로 통지됨). M5(plan agent 도입) 시점에 확정 필요.
- **(재확인 필요) capacity_pools 집계 수준의 적절성**: supply_coordination이
  보는 총량/잔여 수준이 production_plan의 세부 스케줄(changeover 등)에 비해
  너무 거칠어 infeasible이 반복될 위험 — M5에서 negotiation_log 반복
  패턴으로 실증 확인.
