# State 스키마

State의 8개 최상위 필드 정의와 필드 수준의 규칙. 구현 중 실제로 이 스키마
자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일을 직접 고친다. 설명은 한글, 실제 필드명/태그값(스키마 코드블록
안)은 영어(snake_case). agent의 역할·내부 단계는 AGENT_NODE_LIST.md, agent 간 연결·신호·동시성은 GRAPH_FLOW.md 참고.

## 전체 구조 요약

```
forecast agent((회사,item) 인스턴스별 1개, 총 N개, role_tag: forecast) — 지속 태스크
   (가정 정의→데이터 수집·소스 판단→통계기법 선택→가정별 요청량 예측값 계산→
    가정 선택을 한 agent 내부 단계로 수행)
        ↕  (신호 기반, 평소 정방향 최적화 / 예외 시에만 역방향 send-back)
supply_coordination agent(1개, role_tag: supply_coordination) — 지속 태스크
   ↕procurement_plan   ↕production_plan   ↕logistics_plan  (각 1개, 지속 태스크)
   (hub-and-spoke — 셋 다 직접 연결, 사슬 아님. 순서는 의존관계에 따른
    호출 순서일 뿐, 건너뛰기/역방향 send-back 모두 구조적으로 가능)
        ↓
실제 조달/생산/배송 (그래프 노드 아님, 외부 경계 — sales_channel과 같은 성격)

validation agent(들) — 이벤트 트리거·무기억 워커풀 방식으로 일하고,
판정(`passed`/`failed`/`error`)만 State에 쓴다. 기록이 "판정됨" 상태가 되면 작성agent의
채널(작성agent의 `role_tag`)로 신호가 간다 — 라우팅 권한은 없고
다음 agent에게 전달하지 않는다. human_manager(들) — escalation 발생 시 반응(알림은 받기만 함), 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 send-back 또는 escalation할지는 interaction_protocol이 규정.
```

**동시성 모델**: asyncio 단일 프로세스 — 각 지속 태스크가 서로 안 막히고
독립적으로 진행, in-process 신호(asyncio.Queue)로 소통. 상세는 GRAPH_FLOW.md "동시성 모델" 참고.

## 최상위 State 필드

### 1. `forecast_records[]`
forecast agent 인스턴스((회사, item) 조합별 1개)가 State에 남기는 현재값 기록. 다른 agent가 이 값을 읽는다(`role_permissions`로 통제).
```
{ agent_id, company_id, item_id, pool_key, role_tag: "forecast",
  assumptions: [                                            # 가정 목록
    { assumption_id,
      drivers: [                                            # 가정마다 다름. 비어 있으면 기본 가정(현재 추세 유지)
        { driver: "category_trend" | "price" | "event",     # 요청량을 바꾸는 원인(수요 동인)
          evidence: { kind, item_scope, refs } }            # 이 원인의 근거 — data_sources 풀에서 쓰는 데이터
      ],
      defined_by: "rule" | "agent_judgment",
      method_values: [                                      # 이 가정이 고른 통계기법별 요청량
        { method, value, method_weight } ],                 # method_weight: 이 가정에서 잰 과거 정확도로 구한 기법 가중치
      value,                                                # 기법별 값을 합치거나 하나 선택한 가정의 요청량
      occurrence_likelihood,                                # 가정 발생 가능성(근거가 있을 때만, 없으면 null)
      forecast_uncertainty }                                # 기법별 과거 오차(MAE)를 기법 가중치로 가중한 값
  ],
  premises: [ { driver: "event", evidence } ],              # 모든 가정의 전제(확정된 프로모션 일정). 가정의 요소가 아님
  excluded_drivers: [
    { driver, assumption_ids, reasons, rationale }          # 근거 부족·효과 없음·반영 불가·재실행으로 제외한 원인과 영향받은 가정
  ],                                                        # reasons: ["no_significant_effect" | "no_evidence" | "no_applicable_method" | 의심되는 원인, ...]
  excluded_assumptions: [
    { assumption_id, reasons: ["no_applicable_method" | 의심되는 원인, ...],  # 맞는 통계기법이 없거나 의심되는 원인으로 제외한 가정
      rationale }
  ],
  data_sources: [
    { kind: "orders" | "pos" | "market",                   # 데이터 출처
      item_scope: "same_item" | "similar_item" | "category",  # 예측 대상 item과의 관계
      refs: [...],                                          # 실제로 참고한 item/출처
      use_from: date | null }                               # 이 날짜 이전 데이터는 쓰지 않음
  ],
  excluded_sources: [
    { kind, item_scope, refs, reason: "irrelevant" | "contaminated" }  # 쓰지 않는 데이터(관련 없음 또는 오염) — 재실행 시 다시 고르지 않음
  ],
  cleaning: { applied, count },                             # 표준 정제 적용 기록
  scenario: { value, assumption_ids, derivation } | null,   # 가정들 중에서 택하거나 계산해 정한 최종 요청량과 출처 가정
                                                            # derivation: "pass_through" | "chosen" | "mean" | "median"
                                                            # 요청량을 만들 수 없으면 null(아래 "요청량을 만들 수 없을 때")
  selection_basis: "rule" | "agent_judgment" | "human" | null,
  validation: { status: "passed" | "failed" | "error",
                suspected_causes: [suspected_cause],         # failed일 때 1개 이상, 그 밖에는 빈 목록
                rationale, ts, validator_role_tag },
  forward_to: role_tag | null,                              # passed를 확인한 작성agent가 다음 agent를 정해 쓴다
  send_back: { from_role, suspected_causes: [suspected_cause], ts } | null  # 기록을 받은 쪽이 기록을 쓴 agent에게 되돌릴 때 쓴다
}

suspected_cause: {
  type:   "assumption" | "data_source" | "method_selection",
  issue:  (문제 대상별 문제 내용 — 아래 참고) | null,
  assumption_id: ... | null,                                  # type이 assumption일 때 대상 가정(필수). 다른 type에는 없다
  drivers: [driver] | null,                                  # type이 assumption일 때만. 문제 원인을 지목할 수 있으면 그 원인들, 지목할 수 없으면 null(그 가정의 모든 원인)
  source: { kind, item_scope } | null,                       # type이 data_source일 때 문제가 난 소스
  use_from: date | null                                      # outdated일 때, 이 날짜 이전 데이터는 쓰지 않음
}
```

인스턴스 단위는 (회사, item) 조합이다 — 한 회사가 여러 item(예: 라면과
과자)을 동시에 주문할 수 있어, 회사 단위로만 나누면 item별로 다른 상태
(하나는 정상, 하나는 데이터 소스 문제)를 표현할 수 없다. `agent_id`는
`"{company_id}:{item_id}"` 같은 합성키로 두되, 파싱 대상이 아니라 표시용
키로만 쓰고, 실제 필터링/조회는 `company_id`/`item_id` 필드로 한다.
`company_id`는 **필수이며 null을 허용하지 않는다** — 고객사가 정해지지 않은 수량(예: 신제품 첫
물량)은 forecast를 거치지 않는 human_input 수요로 들어온다(아래 "아직 정하지 않은 것" 참고).

`pool_key`는 forecast 단계에서 agent가 직접 정하는 값이 아니다 — 어느
생산라인에 배정될지는 production_plan의 판단이므로, `allocation_candidates[i].exchanges`의 production_plan 응답으로 정해진 뒤 참조용으로 기록만 
된다(아래 `capacity_pools` 절 참고).

**용어 — 이 문서가 정의하고 다른 문서는 참조한다.**
- **driver**(수요 동인): 요청량을 바꾸는 원인 하나. 프로모션(`event`), 시장 추세(`category_trend`) 등.
  근거 데이터가 실제로 있는 것만 목록에 두고 새 원인이 필요해지면 값을 추가한다. 자유 텍스트로 두면
  근거를 검증할 수 없다. `price`(매장 판매가 변화)는 확장 영역이다 — 매장 판매가 데이터가 없고 고객사 단독
  가격 조정은 미리 알 수 없어 `price`는 매장 판매가 데이터가 생길 때 쓴다. 협의된 가격 인하는 프로모션이라
  `event`로 들어간다. 최소 구매 약정은 이미 맺은 계약 수량으로 supply_coordination의 입력이다(AGENT_NODE_LIST.md).
- **가정**(assumption): 선택지 1개. driver 몇 개와 통계기법 여러 개를 엮어 계산한 하나의 경우의 수다.
  `drivers[]`가 가정마다 다르고 비어 있으면 기본 가정이다.
- **시나리오**(scenario): 가정들 중에서 하나를 택하거나 계산해서 정한 **최종 요청량**과 어느 가정에서 왔는지(`scenario`). 가정 선택 단계가 만든다.
- **data_sources**(forecast_record 바로 아래): 수집과 소스 판단을 거친 데이터 풀. 가정 안 `evidence`가
  그 풀에서 어떤 데이터를 쓰는지 `(kind, item_scope)`로 가리킨다("근거가 실제로 수집된 데이터에 있는가"를 바로 확인할 수 있게 같은 어휘를 쓴다).

**가정의 구성.** (필드 수준의 규칙만 둔다. 가정을 어떻게 정하고 계산하는지는 AGENT_NODE_LIST.md forecast 내부 단계에 있다.)
- 기법별 값(`method_values`)은 가정 안에만 둔다. 기본 가정(`drivers`가 빈 가정)도 같은 구조이며 기법별 값은 우리 주문 이력만으로 계산한 값이다.
- 가정 안의 두 값은 이름이 다르다. `method_weight`는 그 가정 안에서 기법의 과거 정확도로 구한 기법
  가중치이고, `occurrence_likelihood`는 가정이 실제로 일어날 가능성이다(근거가 있을 때만 채우고 M2에서는
  채우는 규칙이 없어 null이다). 발생 가능성은 가정의 `occurrence_likelihood`로만 둔다.
- `value`는 가정 하나의 요청량 예측값이고, 가정 안에서 기법별 값을 합치거나 하나 선택해 정한다.
- `defined_by`는 가정을 규칙이 만들었는지(`"rule"`) agent 판단이 만들었는지(`"agent_judgment"`)를 기록한다.
- 가정 정의 직후에는 `method_values`와 `value`, `forecast_uncertainty`가 비어 있고(가정 정의는 원인과
  근거를 선언만 한다) 통계기법 선택과 가정별 요청량 예측값 계산이 채운다. 값이 비어 있는 가정은 가정 선택의 대상이 아니다.
- 확정된 프로모션 일정은 가정의 요소가 아니라 **모든 가정의 전제**(`premises`)다.
- 제외 기록: 가정에서 제외한 원인은 `excluded_drivers`에 원인, 영향받은 가정, 제외 이유 목록(`reasons`)을 남기고,
  계산할 수 없어 제외한 가정은 `excluded_assumptions`에 제외 이유 목록(`reasons`)을 남긴다. 한 원인이나 가정에 이유가
  여럿이면 모두 넣는다. 재실행으로 제외한 원인과 
  가정은 `reasons`에 의심되는 원인(`suspected_cause`의 문제 대상·문제 내용)를 `{type}:{issue}` 형식으로 적고,
  문제 내용이 없으면 `{type}`으로 적는다(예: `assumption:value_out_of_range`, `data_source:outdated`,
  `method_selection`). `excluded_drivers.reasons`의 값은 `no_significant_effect`(신뢰구간이 0을 포함),
  `no_evidence`(forecast가 근거 데이터를 수집하지 못했거나 기록이 모자람), `no_applicable_method`(전제를
  설명변수로 받는 기법이 계산되지 않음) 중 하나이거나 의심되는 원인다. `excluded_assumptions.reasons`의 값은
  `no_applicable_method`이거나 의심되는 원인다. 의심되는 원인의 문제 내용(`suspected_cause.issue`)
  `no_evidence`는 검증agent가 가정의 근거가 이번 주기 스냅샷의 `data_sources`에 없다고 알리는 값이며, 두
  `no_evidence`는 쓰이는 필드로 구분한다(`excluded_drivers.reasons`와 `suspected_cause.issue`).
- **요청량을 만들 수 없을 때**: 계산된 가정이 하나도 없으면(기본 가정까지 계산되지 않았거나, 사용할 수 있는
  주문 이력이 없거나, 재실행 전부터 `scenario`가 없었음) `scenario`와 `selection_basis`는 `null`이다.
  재실행 전에 계산된 `scenario`가 있었는데 재실행으로 가정이 모두 제외되면 `scenario`(1개)와 `assumptions`(그
  `scenario`를 만든 가정 목록)를 재실행 전 값 그대로 두고 escalation 기록의 `reason`은 `options_exhausted`다. 그 가정들은
  `excluded_assumptions`에도 있을 수 있다. 요청량 0은 정상 값(`scenario.value = 0`)이고
  `null`은 값이 없다는 뜻이라 서로 다르다. 이때 `escalation_records`에 `reason: "no_computable_assumption"`
  escalation 기록이 만들어진다(7번 절). 이후 forecast agent의 동작은 AGENT_NODE_LIST.md "사람 escalation 세 경우"를 따른다.
- 가정 검증 조건(검증agent가 확인하는 독립 제약조건)은 AGENT_NODE_LIST.md 검증agent 절에 있고, 어긴 경우의
  문제 내용 값은 아래 `suspected_cause` 표에 있다.

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
제품. 현재 데이터로 채울 수 있는 조합은 AGENT_NODE_LIST.md forecast agent "입력" 참고.

**의심되는 원인(`suspected_cause`)** — `type`은 문제 대상, `issue`는 그 대상의 문제 내용:

| 문제 대상(`type`) | 문제 내용(`issue`) |
|---|---|
| `assumption` | `no_evidence`(근거가 스냅샷에 없음) / `value_out_of_range`(가정의 value가 과거 월별 요청량 범위를 벗어남) / `not_distinct`(같은 구성의 가정이 둘 이상) / `double_counted`(한 가정 안 원인 간 근거 중복) — 위 가정 검증 조건과 1:1 대응 |
| `data_source` | `insufficient`(부족) / `contaminated`(오염) / `irrelevant`(소스 자체가 무관) / `outdated`(시점 기준으로 무관 — `use_from` 필수) |
| `method_selection` | 없음(가정마다 고른 통계기법 구성이 문제) |
| `misrouted` | 없음(받은 agent가 자기 일이 아니라고 판단함. `send_back`에만 쓰고 `validation`에는 쓰지 않으며, 다른 이유와 함께 보내지 않는다) |

대상 필드는 문제 대상(`type`)별로 정해져 있다. `assumption`에는 `assumption_id`가 필수이고 다른 문제 대상에는
`assumption_id`와 `drivers`가 없다. `drivers`는 문제 내용으로 지목 가능 여부가 정해진다: `no_evidence`는 근거가 없는
원인들, `double_counted`는 근거가 겹치는 원인들을 넣고, `value_out_of_range`와 `not_distinct`는 원인을 지목할 수 없어
항상 null이다. 빈 목록은 허용하지 않는다(모든 원인은 null로 나타낸다). `data_source`는 `insufficient` 이외의 문제 내용에 `source`가 필요하다. `use_from`은 `outdated`에 필수이고 다른 문제 내용에는 없다. `orders`는 항상 쓰므로 소스를 제외할 수 없어 `orders`는
`outdated`만 받고, `orders`의 `irrelevant`는 허용되지 않는 판정이다. `method_selection`은 문제 내용과 대상 필드가
없다. 이 필드로 정해지는 대상 가정과 재실행의 범위는 AGENT_NODE_LIST.md forecast agent "재실행"에 있다.

의심되는 원인별 재개 지점과 대응은 AGENT_NODE_LIST.md forecast agent "재실행" 참고(`misrouted`는 재실행하지 않는다). send-back·불합격 판정·
재실행 용어는 GRAPH_FLOW.md "send-back·재실행 관련 용어" 참고.
검증agent의 불합격 판정(`validation`)과 `send_back`은 같은 형식의 이유를 싣는다. 한 번에 여러 이유를 목록(`suspected_causes`)으로 보낸다.

`validation`은 **현재값만** 유지(이력 전체는 `role_logs`에 쌓임 —
판단용 현재값과 기록용 스냅샷 분리). 덮어쓰기 전 값은 재실행의 `rerun` 항목 `payload`에 남는다.

`forward_to`는 검증agent의 판정이 `passed`임을 확인한 작성agent가 다음 agent의 role_tag를 골라 써서 넘긴다. 작성agent의 카드(8번 절)
`known_agents`에 있는 role_tag만 쓸 수 있고 그 밖의 값은 쓰기가 거부된다. `send_back`은 기록을 받은 쪽이 기록을 쓴 agent(작성agent)에게
되돌릴 때 쓰며, 되돌림은 항상 작성agent에게 간다. `suspected_causes`는 `validation`의 것과 같은 형식이다(검증agent의 `failed`는
`validation`에, 받은 agent의 되돌림은 `send_back`에 쓴다).
작성agent가 재실행 결과를 쓸 때는 아직 검증받지 않았으므로 `validation`, `forward_to`, `send_back`을 모두 비운다.
이 필드들로 정해지는 기록의 상태와 신호는 GRAPH_FLOW.md "신호 규칙"에 있다.

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
공유하는가** 기준으로 묶는다 — 같은 카테고리여도 라인이 다르면 다른 `pool_key`.

**capacity_pools의 실제 접근 주체는 supply_coordination과
production_plan뿐이다.** procurement_plan·logistics_plan은 `capacity_pools`를
쓰지 않는다 — 이 둘의 제약은 "여러 요청이 실시간으로 같은 잔여량을
나눠 갖는" 계좌형 공유 자원이 아니라, 요청마다 확률분포로 독립적으로
feasible/infeasible을 응답하는 구조이기 때문(AGENT_NODE_LIST.md 각 agent의
데이터 소스 참고). logistics_plan은 아직 agent 자체가 미구현이라(M5) 권한 설정도 그때 정해진다.

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
      routing_reason,  # infeasible일 때 supply_coordination이 어디로/왜 send-back했는지
      validation: { status: "passed" | "failed" | "error", suspected_causes, rationale, ts, validator_role_tag } },
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

우선순위 대기열 항목(산출 규칙은 AGENT_NODE_LIST.md supply_coordination agent 참고):
```
priority_queue_entry = { agent_id, wait_start_ts, revenue_impact,
  forecast_reliability, aging_adjustment, priority_score }
```

### 4. `role_logs{}`
역할별 로그 `{role_tag: [entry]}`가 원본이다. `exchanges`가 "한 역할과의 요청-응답 쌍"을 담는 개별 트랜잭션이라면, 로그는
조달·생산·배송처럼 **서로 다른 역할들의 이벤트가 시간순으로 쌓이는 기록**(DB의 트랜잭션 로그와 같은 역할)이다. Step1 원칙3
(판단용 현재값 vs 기록용 스냅샷 분리) 재적용: `exchanges`·`forecast_records`=판단에 쓰는 현재 상태, 로그=기록(감사·역추적용).
(LangSmith와 별개 — 이건 agent 판단이 실제로 참조하는 데이터, LangSmith는 사람이 보는 디버깅용)
```
role_logs: { "{role_tag}": [ { seq, ts, role_tag, agent_id | null, event, round | null, payload } ] }
```
- **쓰기**: 각 역할은 자기 로그(`role_logs.{role_tag}`)에만 쓴다(`role_permissions`). 로그 쓰기 함수가 `seq`와 `ts`를 붙인다.
  `seq`는 모든 역할을 통틀어 하나로 늘어나는 순번이다.
- **`event`는 정해진 사건 이름만 쓴다.** 값(`agent_id`, `plan_id`, 의심되는 원인, 판정 시각 등)은 `event` 문자열에 넣지
  않고 `agent_id` 필드와 `payload`에 담는다. 사건 이름: forecast는 `scenario_decided`, `scenario_not_computable`,
  `scenario_options_exhausted`, `rerun`, `forwarded`, `next_agent_unresolved`, `forecast_run_error`, `forecast_run_skipped`,
  supply_coordination은 `allocation_candidate_generated`, `misrouted_send_back`(받은 기록을 자기 일이 아니라고 되돌림), 검증agent는 `validation_passed`, `validation_failed`,
  `validation_error`다.
- **`payload`에는 역추적에 필요한 값을 담는다.** forecast 재실행(`rerun`)은 재실행의 계기(`trigger`: `failed_verdict` 불합격 판정 또는 `send_back`), 덮어쓰기 전의 `assumptions`,
  `scenario`, `selection_basis`, `validation`과 의심되는 원인을 담고, 판정(`validation_*`)은 `suspected_causes`와
  `rationale`과 판정 시각(`validation_ts`)을 담는다. `forecast_run_error`는 실패한 단계와 예외 내용을 담는다. `forwarded`는 판정 시각과
  `forward_to`를, `next_agent_unresolved`는 `reason`(`no_next_agent`/`multiple_next_agents`)과 후보와 제외한 agent를, `misrouted_send_back`은
  되돌린 agent(`from_role`)와 그 기록의 판정 시각(`validation_ts`)을 담는다.
- **`negotiation_log`는 저장하지 않는다.** `role_logs`의 항목을 `seq` 순으로 합쳐 읽는 함수의 결과이고, 역할 사이에서
  이벤트가 어떻게 겹치거나 어긋나는지 볼 때 쓴다. 이력을 읽는 곳은 모두 이 함수를 쓴다.
- **로그를 판단에 쓰는 곳**(예: 같은 기록에 `misrouted`로 되돌린 agent 찾기)은 `event` 문자열의 값을 해석하지 않고 사건 이름과
  `agent_id`와 `payload`의 필드로 조회한다.

검증 이벤트도 별도 필드 없이 여기에 함께 쌓임 — "통과"/"이상감지" 각각의 현재값은 해당 record
(`forecast_records[i].validation` 등)에 있고, 그 이력(언제 통과였다가 언제 번복됐는지)은 합쳐 읽은 로그를 시간순으로 읽으면 됨.

### 5. `interaction_protocol[]`
이 표는 코드가 조회하는 엣지별 기준값(max_rounds, repeat_escalation_threshold, 알림 기준, 처리 모드 등)을 담는다. 트레이싱·상호작용 기록이나 외부 벤치마크로 조정할 값이 있는 엣지만 두고, 값이 고정인 경우는 두지 않는다. 엣지의 흐름은 GRAPH_FLOW.md에 있다.
agent 역할 간 상호작용 규칙(동역학). `scope`는 인스턴스 나열이 아니라 역할태그.
```
{ edge: "supply_coordination<->procurement_plan", max_rounds: 3,  # 타임아웃 안전장치
  repeat_escalation_threshold: 2,  # 같은 이유(routing_reason 등)가 이 횟수만큼
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
단방향 전달=최적화, 역방향은 공급망계획agent의 infeasible 신호로 supply_coordination이
forecast에 send-back을 보내는 핸드오프형 — GRAPH_FLOW.md 엣지 표 참고), **두 방향 모두
라운드가 쌓이지 않아 `max_rounds`가 필요 없다** — 그래서 정방향/역방향을
나누지 않고 레코드 하나를 함께 쓴다:
```
{ edge: "forecast<->supply_coordination", repeat_escalation_threshold: 2,
  scope: ["forecast", "supply_coordination"],
  escalation_trigger, escalation_target, escalation_kind,
  source: "initial_design", last_updated }
  # max_rounds 없음: 정방향(최적화)·역방향(핸드오프형) 모두 라운드 개념이
  # 없다. repeat_escalation_threshold는 forecast 쪽 failed에는 쓰지 않는다
  # (forecast의 재실행은 스스로 끝나서 failed의 반복을 셀 필요가 없다).
  # supply_coordination 기록이 failed로 판정될 때 이 값이 필요한지는 M4에서 정한다
```
다른 엣지(supply_coordination↔procurement_plan 등)는 요청→응답→
(infeasible 시) 라운드 연장이 실제로 있어 위 첫 예시처럼 `max_rounds`를
쓴다. `InteractionProtocol` pydantic 모델에서 `max_rounds`를 필수에서
선택(Optional)으로 바꿀지는 이 구분을 실제로 구현하는 마일스톤에서 정한다.

**supply_coordination → human_manager**(최소 구매 약정과 배분의 차이,
AGENT_NODE_LIST.md supply_coordination agent 참고)는 plan agent와의 협상과 무관한
별도 엣지다. 진행을 멈추지 않는 `notice` 모드라 라운드가 없고, 알림 기준값만
둔다. 약정의 구속력에 따라 기준이 다르다(구속력이 없으면 우리가 손실을 떠안으므로
더 작은 차이에도 알림). 기록만 해 두고 구현은 M4·M5이며, 처리 규칙과 아래 기준값의
측정 대상은 그때 정한다:
```
{ edge: "supply_coordination->human_manager", scope: ["supply_coordination", "human_manager"],
  escalation_trigger: "commitment_gap", escalation_target: "human_manager",
  escalation_kind: "rule", escalation_mode: "notice",
  notice_threshold: { binding: 0.2, non_binding: 0.05 },  # 약정 잔여량과의 차이 비율
  source: "initial_design", last_updated }
```

**forecast → human_manager**는 가정 선택 ③에만 항목이 있다. ③은 가정들의 값이 서로 많이 다르고, 과거
정확도로 매긴 점수에서도 1등 가정이 2등을 뚜렷하게 앞서지 못해 어느 가정을 믿을지 정할 수 없을 때 사람을
부르는 경우다(판단 기준은 AGENT_NODE_LIST.md forecast 5단계). 사람이 필요한지(모드)를 트레이싱·처리 결과를
보고 조정할 대상이기 때문에 항목을 둔다. 요청량을 만들 수 없을 때, 재실행으로 가정이 모두 제외됐을 때, 다음 agent를 정하지 못했을 때는 조정할 값이
없고 항상 `intervention`이라 항목을 두지 않는다. 같은 엣지에 트리거가 여러 개일 수 있어 조회 키는
`(edge, escalation_trigger)`다. ③의 수치 기준은 forecast 내부 판단 기준이라 `judgment_thresholds.py`에 둔다.
```
{ edge: "forecast->human_manager", scope: ["forecast", "human_manager"],
  escalation_trigger: "selection_unresolved", escalation_target: "human_manager",
  escalation_kind: "rule", escalation_mode: "intervention",
  source: "initial_design", last_updated }
  # max_rounds, repeat_escalation_threshold 없음: 이 엣지에는 라운드가 없다
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
에서 자기 `role_tag`에 해당하는 항목만 r/w. `agent_cards`는 카드를 가진 agent가 읽는다(r). `capacity_pools`는
`supply_coordination`(r, 배분 판단용)과 `production_plan`(r/w, 자기
라인 자원이므로)만 접근한다.

`field_path`의 인스턴스 인덱스는 와일드카드 `[*]`를 쓸 수 있다. 예: 검증agent `forecast_validation`은
`forecast_records[*].validation`에만 쓸 수 있어 모든 인스턴스의 판정 기록만 쓰고 다른 필드는 못 쓴다. 인덱스가
없는 `field_path`(`forecast_records`)는 모든 인덱스와 그 아래 필드를 포함한다.

### 7. `escalation_records[]`
사람(human_manager)에게 올라간 escalation 기록. `mode`로 종류를 구분한다 —
`intervention`은 진행을 멈추고 사람의 결정을 기다리는 escalation 기록, `notice`는 알리기만
하고 진행하는 escalation 기록(사람의 결정이 없으므로 `resolution`이 없음).
```
{ agent_id | null,                       # escalation 기록이 발생한 인스턴스("{company_id}:{item_id}"). 인스턴스와 무관한 엣지는 null
  trigger_edge, reason, rationale,       # rationale: 사람이 읽는 이유 설명
  target_role: "human_manager",
  mode: "intervention" | "notice",
  log_seq,               # 이 escalation과 관련된 로그 항목의 seq(`role_logs`, 4번 절)
  status, resolution }   # resolution은 intervention일 때만
```
`log_seq`가 가리키는 로그 항목은 escalation 사유를 뒷받침하는 값을 담는다. `forecast_run_error`는 예외 내용이 담긴 로그 항목
(`forecast_run_error`)을, 그 밖의 경우는 escalation 시점의 값이 담긴 로그 항목을 가리킨다(예: `no_computable_assumption`은
제외된 가정과 이유, `selection_unresolved`는 그때의 가정 값과 시나리오, `options_exhausted`는 유지한 시나리오와 가정 목록,
`validation_error`는 검증 오류 내용). 보류에 들어갈 때 로그 항목, escalation 기록, forecast 기록(있으면)을 한 번의 State 갱신으로 쓰고, 보류 중인 인스턴스는
실행하지 않으므로 그때의 값은 이 로그 항목에서 찾는다.
긴급도(`emergency`/`warning`)는 State 필드가 아니라 코드 안의 reason→긴급도 대응표로 정한다. `emergency`는 진행할 값이 없거나
시스템 장애인 경우(`validation_error`, `no_computable_assumption` 등), `warning`은 값은 있지만 확인이 필요한 경우
(`selection_unresolved`, `no_next_agent`, `multiple_next_agents`, 약정과 배분의 차이 알림 등)다.

`reason`은 엣지마다 값 목록이 다르다. `forecast->human_manager`의 값은 여섯 가지다: `no_computable_assumption`(계산된
가정이 하나도 없어 요청량을 만들 수 없음), `selection_unresolved`(가정 선택 ③), `options_exhausted`(재실행 전에
계산된 `scenario`가 있었는데 재실행으로 가정이 모두 제외됨. `scenario`와 `assumptions`는 재실행 전 값을 그대로 둠), `forecast_run_error`(forecast 실행이 예외로 끝남. 기록은
이전 값 그대로), `no_next_agent`(`passed` 뒤에 넘길 후보 agent가 없음), `multiple_next_agents`(후보가 여럿이라 고르지 못함. M7 전까지).
`no_next_agent`와 `multiple_next_agents`는 기록의 값을 그대로 두고 `forward_to`는 비어 있다. 여섯 경우 모두 `mode`는 `intervention`이고
`status`는 `"open"`으로 시작한다.
`{validator_role_tag}->human_manager`(검증agent가 시스템 장애로 사람에게 올리는 엣지)의 값은 `validation_error`
하나다(검증 절차가 비정상 종료함). `mode`는 `intervention`, `status`는 `"open"`으로 시작하며 `agent_id`는
검증 대상 인스턴스다.

### 8. `agent_cards[]`
경로 위 agent(supply_coordination, forecast처럼 기록을 받고 넘기는 agent)마다 카드 하나. `interaction_protocol`, `role_permissions`와 같은 층의
설정 데이터이고, 다음 agent 선택(GRAPH_FLOW.md "다음 agent 선택")과 받는 쪽 반송의 기준이다. 검증agent와 human_manager는 경로 밖이라 카드가 없다.
```
{ role_tag,
  description,                   # 이 agent가 하는 일(M7의 LLM이 후보 중에서 고를 때 읽음)
  accepts: string | null,        # 받는 것(넘겨받는 기록의 종류). 받지 않으면 null
  produces: string | null,       # 내는 것(넘기는 기록의 종류). 넘기지 않으면 null
  response_type: "optimization" | "handoff" | "round_accumulation",  # 넘겨받은 기록에 응답하는 방식
  known_agents: [role_tag] }     # 이 agent가 넘길 수 있는 agent
```
- `response_type`은 GRAPH_FLOW.md "상호작용 세 가지 유형"의 이름이다(`optimization` 최적화, `handoff` 핸드오프형, `round_accumulation` 라운드 누적형).
- 두 agent가 이어지는지는 보내는 쪽의 `known_agents`에 받는 쪽이 있고 받는 쪽의 `accepts`가 보내는 쪽의 `produces`와 같은지로 본다.
- 지금 카드: `forecast`(`produces`: "검증을 통과한 수요 예측", `known_agents`: `[supply_coordination]`), `supply_coordination`(`accepts`: "검증을
  통과한 수요 예측", `known_agents`: `[]`). plan agent 카드와 supply_coordination의 `known_agents`는 M5에서 더한다.

## State 접근 규칙

- 모든 State 읽기/쓰기는 `role_permissions`를 검사하는 wrapper 함수(예:
  `get_field(role_tag, field_path)`, `set_field`, `update_state`)를 통해서만 한다 — 코드가
  다른 role의 영역을 직접 건드리는 걸 런타임에 막기 위함.
- 여러 곳을 함께 쓸 때(기록과 그 로그 항목, 판정과 로그 항목과 escalation 기록 등)는 `update_state`를 쓴다. 쓸 곳마다 쓰기 권한을
  먼저 확인하고, 모든 쓰기를 한 번의 State 갱신으로 반영한다(전부 쓰거나 하나도 쓰지 않는다). `set_field`는 필드 하나를 쓰는
  `update_state`다. 신호는 쓰는 쪽이 지정하지 않고 기록의 상태가 바뀔 때 자동으로 간다(GRAPH_FLOW.md "신호 규칙").
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
- **공급 보장 물량 미충족 시 escalation과 sales_channel "조정 불가" 통지의
  순서**: escalation이 먼저 사람에게 조치 기회를 준 뒤에만 sales_channel로
  "조정 불가"가 나가야 함(순서가 반대면 사람이 풀 수 있었던 건이 이미
  불가로 통지됨). M5(plan agent 도입) 시점에 확정 필요.
- **(재확인 필요) capacity_pools 집계 수준의 적절성**: supply_coordination이
  보는 총량/잔여 수준이 production_plan의 세부 스케줄(changeover 등)에 비해
  너무 거칠어 infeasible이 반복될 위험 — M5에서 negotiation_log 반복 패턴으로 실증 확인.
- **forecast의 사람 escalation 처리 (M4)**: `resolution`의 모양(사람이 넣는 요청량 등), 응답을 기다리는 시한과
  시한이 지났을 때의 처리, `status` 값 목록. M2는 `status: "open"`으로 기록하는 데까지만 한다.
  forecast를 거치지 않는 수요(human_input)의 State 반영 구조와 함께 정한다.
