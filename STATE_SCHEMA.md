# State 스키마

State의 9개 최상위 필드 정의와 필드 수준의 규칙. 구현 중 실제로 이 스키마
자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일을 직접 고친다. 설명은 한글, 실제 필드명/태그값(스키마 코드블록
안)은 영어(snake_case). agent의 역할·내부 단계는 AGENT_NODE_LIST.md, agent 간 연결·신호·동시성은 GRAPH_FLOW.md 참고.

## 전체 구조 요약

```
forecast agent((회사,item) 인스턴스별 1개, 총 N개, role_tag: forecast) — 지속 태스크
   (가정 정의→데이터 수집·소스 판단→통계기법 선택→가정별 요청량 예측값 계산→
    가정 선택을 한 agent 내부 단계로 수행)
        ↓  예측 수요 전달(신호 기반) — 예외 시에만 역방향 send-back
supply_coordination agent(1개, role_tag: supply_coordination) — 지속 태스크
   (생산라인마다 예측 수요·수주·사람이 정한 수량을 계약 조건과 공급 가능량 안에서
    배분해 결론을 allocations에 씀)
        ↕  (M5) 회의(meetings)로 procurement_plan · production_plan · logistics_plan
            (각 1개, 지속 태스크)과 연결 — 참여 agent가 서로의 기록을 보고 판단
        ↓
sales_channel(외부 경계)이 완료된 allocations를 읽어 감 / 실제 조달·생산·배송(외부 경계)

validation agent(들) — 이벤트 트리거·무기억 워커풀. agent가 맡은 일이 끝났을 때
판정(`passed`/`failed`/`error`)만 State에 쓴다. 기록이 "판정됨" 상태가 되면 작성agent의
채널(작성agent의 `role_tag`)로 신호가 간다 — 라우팅 권한은 없고 다음 agent에게 전달하지 않는다.
human_manager(업무 판단) · system_operator(시스템 오류) — escalation 발생 시 반응
(알림은 받기만 함), 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 send-back 또는 escalation할지는 interaction_protocol이 규정.
```

**동시성 모델**: asyncio 단일 프로세스 — 각 지속 태스크가 서로 안 막히고
독립적으로 진행, in-process 신호(asyncio.Queue)로 소통. 상세는 GRAPH_FLOW.md "동시성 모델" 참고.

## 최상위 State 필드

### 1. `forecast_records[]`
forecast agent 인스턴스((회사, item) 조합별 1개)가 State에 남기는 현재값 기록. 다른 agent가 이 값을 읽는다(`role_permissions`로 통제).
```
{ agent_id, company_id, item_id, role_tag: "forecast",
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
                                                            # derivation: "pass_through" | "chosen" | "mean" | "median" | null
                                                            #   (null: 사람이 escalation을 처리해 넣은 값 — 가정에서 만든 값이 아니다)
                                                            # 요청량을 만들 수 없으면 null(아래 "요청량을 만들 수 없을 때")
  selection_basis: "rule" | "agent_judgment" | "human" | "expiry" | null,
                                                            # human: 사람이 escalation을 처리해 넣은 값
                                                            # expiry: escalation 시한이 지나 정해진 처리로 진행한 값
                                                            # human·expiry는 검증 면제 통과(GRAPH_FLOW.md "신호 규칙", 7번 절)
  validation: { status: "passed" | "failed" | "error",
                suspected_causes: [suspected_cause],         # failed일 때 1개 이상, 그 밖에는 빈 목록
                rationale, ts, validator_role_tag } | null,  # 검증agent의 판정만 쓴다
  forward_to: role_tag | null,                              # passed 또는 검증 면제 통과를 확인한 작성agent가 다음 agent를 정해 쓴다
  send_back: { from_role, suspected_causes: [suspected_cause], ts } | null  # 기록을 받은 쪽이 기록을 쓴 agent에게 되돌릴 때 쓴다
}

suspected_cause: {
  type:   "assumption" | "data_source" | "method_selection" | "allocation",
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
물량)은 forecast를 거치지 않고 사람이 정한 수량으로 supply_coordination에 바로 들어간다(AGENT_NODE_LIST.md "외부 경계").

forecast 기록에는 이 인스턴스가 어느 생산라인(풀)에 속하는지를 두지 않는다. `item_id`로 라인 기준 정보에서
찾는 값이라(2번 절) 따로 저장하면 원본과 어긋날 수 있다.

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
| `allocation` | `exceeds_supply`(배분 합계가 그 라인의 공급 가능량을 넘음 — 배분 결론 검증 조건과 1:1 대응. `allocations`의 `validation`에만 쓴다) |
| `misrouted` | 없음(받은 agent가 자기 일이 아니라고 판단함. `send_back`에만 쓰고 `validation`에는 쓰지 않으며, 다른 이유와 함께 보내지 않는다) |

대상 필드는 문제 대상(`type`)별로 정해져 있다. `assumption`에는 `assumption_id`가 필수이고 다른 문제 대상에는
`assumption_id`와 `drivers`가 없다. `drivers`는 문제 내용으로 지목 가능 여부가 정해진다: `no_evidence`는 근거가 없는
원인들, `double_counted`는 근거가 겹치는 원인들을 넣고, `value_out_of_range`와 `not_distinct`는 원인을 지목할 수 없어
항상 null이다. 빈 목록은 허용하지 않는다(모든 원인은 null로 나타낸다). `data_source`는 `insufficient` 이외의 문제 내용에 `source`가 필요하다. `use_from`은 `outdated`에 필수이고 다른 문제 내용에는 없다. `orders`는 항상 쓰므로 소스를 제외할 수 없어 `orders`는
`outdated`만 받고, `orders`의 `irrelevant`는 허용되지 않는 판정이다. `method_selection`은 문제 내용과 대상 필드가
없고, `allocation`은 문제 내용 `exceeds_supply`만 받으며 대상 필드가 없다. 이 필드로 정해지는 대상 가정과 재실행의 범위는 AGENT_NODE_LIST.md forecast agent "재실행"에 있다.

의심되는 원인별 재개 지점과 대응은 AGENT_NODE_LIST.md forecast agent "재실행" 참고(`misrouted`는 재실행하지 않는다). send-back·불합격 판정·
재실행 용어는 GRAPH_FLOW.md "send-back·재실행 관련 용어" 참고.
검증agent의 불합격 판정(`validation`)과 `send_back`은 같은 형식의 이유를 싣는다. 한 번에 여러 이유를 목록(`suspected_causes`)으로 보낸다.
`allocation` 유형은 `allocations`의 `validation`에만 쓴다 — forecast 기록의 `validation`과 `send_back`에는 들어올 수 없고, `allocations`의
`validation`은 `allocation` 유형만 싣는다. 쓰기 규칙을 어기면 State 쓰기가 거부된다.

`validation`은 **현재값만** 유지(이력 전체는 `role_logs`에 쌓임 —
판단용 현재값과 기록용 스냅샷 분리). 덮어쓰기 전 값은 재실행의 `rerun` 항목 `payload`에 남는다.
`validation`에는 검증agent의 판정만 쓴다. 사람이 escalation을 처리해 값을 넣거나 시한이 지나 정해진 처리로 진행한 기록은
검증agent를 다시 거치지 않으므로 `validation`이 비어 있고 `selection_basis`(`human`/`expiry`)로 구분한다(검증 면제 통과, 7번 절).

`forward_to`는 기록이 `passed` 또는 검증 면제 통과(GRAPH_FLOW.md "신호 규칙")인지
확인한 작성agent가 다음 agent의 role_tag를 골라 써서 넘긴다. 작성agent의 카드(8번 절)
`known_agents`에 있는 role_tag만 쓸 수 있고 그 밖의 값은 쓰기가 거부된다. `send_back`은 기록을 받은 쪽이 기록을 쓴 agent(작성agent)에게
되돌릴 때 쓰며, 되돌림은 항상 작성agent에게 간다. `suspected_causes`는 `validation`의 것과 같은 형식이다(검증agent의 `failed`는
`validation`에, 받은 agent의 되돌림은 `send_back`에 쓴다).
작성agent가 재실행 결과를 쓸 때는 아직 검증받지 않았으므로 `validation`, `forward_to`, `send_back`을 모두 비운다. `selection_basis`도
재실행의 가정 선택이 새로 쓰므로, 사람의 처리나 시한 초과로 넘어간 기록도 재실행 뒤에는 다시 검증 대상이 된다.
이 필드들로 정해지는 기록의 상태와 신호는 GRAPH_FLOW.md "신호 규칙"에 있다.

### 2. `capacity_pools[]`
생산라인 하나의 생산능력을 "계좌"처럼 관리한다(라인 하나 = 풀 하나). 라인은 물건을 만드는 설비이고, 풀은 그 라인이
이번 계획 주기에 낼 수 있는 생산능력의 양이다.
```
{ line_id,                                   # 라인 식별자. 라인 기준 정보(line_assignments)의 line_id와 같은 값
  total_capacity, remaining_capacity,        # 계획 주기당 수량. 증감 가능
  adjustment_history: [{ts, delta, reason}] }
```
- **소속**: forecast 인스턴스나 수요가 어느 풀에 속하는지는 State에 저장하지 않는다. `item_id`로 라인 기준 정보
  (`line_assignments`, 외부 데이터 — AGENT_NODE_LIST.md "외부 경계")에서 라인을 찾고 그 라인의 풀을 쓴다. 한 item은 여러
  라인과 연결될 수 있다(주 라인과 대체 라인). M4는 각 item의 주 라인(`preference: "primary"`)만 쓴다.
- **공유·전용**: 따로 표시하지 않는다. 배분 규칙은 한 풀에 연결된 수요가 몇 개든 같다. 라인이 한 제품만 만들도록 된
  전용 라인인지가 필요해지면(M5 production_plan) 연결 수로 계산하지 않고 라인 자체의 정보(`production_lines`)에 성질로 둔다.
- **초기값**: `total_capacity`는 시작 시점에 그 라인 item들의 평소 월 수요 ÷ 가동률로 정한다. 가동률은 0.8이다(임시값,
  근거 확인 필요). 가동률은 초기값을 계산할 때만 쓰고, 그 뒤 값은 고정이다. 라인을 늘리면 사람이 `adjustment_history`에
  증가(`delta`, 이유)를 넣는다.
- **supply_coordination은 읽기만 한다.** 라인의 결론(`allocations`)은 하나이고 조율할 때마다 덮어쓰므로, 조율이 `remaining_capacity`를
  깎지 않는다. 공급 가능량(재고 + 생산 중 물량 + 이번 주기 잔여 capacity)은 조율할 때마다 이 값으로 다시 계산한다.
- **접근 주체는 supply_coordination(r), 검증agent `allocation_validation`(r — 배분 합계가 공급 가능량을 넘는지 확인한다), production_plan(r/w)뿐이다.** procurement_plan·logistics_plan의 제약은 여러 요청이
  같은 잔여량을 나눠 갖는 계좌형 공유 자원이 아니라 요청마다 확률분포로 응답하는 구조라 이 필드를 참조하지 않는다
  (AGENT_NODE_LIST.md 각 agent의 데이터 소스).

### 3. `allocations[]`
supply_coordination이 조율을 끝내고 쓰는 **확정된 현재 배분**이다. 라인마다 하나다. 조율이 끝날 때만 덮어쓰고 비는 때가
없다 — 조율하는 동안에도 이전 결론이 그대로 있어서 다른 agent와 sales_channel은 확정된 값을 읽는다. 덮어쓰기 전 값은
`role_logs.supply_coordination`의 그 조율 항목 `payload`에 남는다(4번 절). 조율하는 동안 오간 과정은 담지 않는다.
배분 규칙은 AGENT_NODE_LIST.md supply_coordination agent, 조율 시점은 GRAPH_FLOW.md "조율 흐름"에 있다.
```
{ role_tag: "supply_coordination",           # 기록을 쓴 작성agent. 기록 상태와 검증agent 선택(`validation_settings`)이 이 값을 읽는다
  line_id, cycle | null,                     # 어느 라인의 어느 계획 주기 배분인지. 첫 조율 전의 빈 기록은 null
  entries: [
    { demand_ref,                            # 예측 수요면 forecast agent_id, 수주·사람이 정한 수량이면 demand_id
      demand_kind: "forecast" | "order" | "manual",
      company_id | null,                     # 사람이 정한 수량은 고객사가 정해지지 않았을 수 있다
      item_id, quantity,
      due_date | null,                       # 수주만(고객사 도착 기준). 예측 수요·사람이 정한 수량은 그 주기 몫이라 null
      peg: "hard" | "soft" }                 # 강한 묶임(다른 수요로 옮길 수 없음) / 약한 묶임(조율로 옮길 수 있음)
  ],
  log_seq,                                   # 이 결론을 쓴 로그 항목(role_logs.supply_coordination)의 seq
  validation: { status: "passed" | "failed" | "error",
                suspected_causes, rationale, ts, validator_role_tag } | null
}
```
- **빈 기록**: 시스템을 처음 설정할 때 `capacity_pools`의 라인마다 빈 기록(`entries` 비어 있음, `cycle`·`validation` null)을 만든다.
  빈 기록은 값이 없어(`cycle`이 null) 상태가 없고 검증 대기도 아니다. 첫 조율 전에 라인이 보류돼도(`commitments_exceed_capacity` 등)
  그 라인의 기록이 "업무 대기"나 "시스템 대기"가 되어 담당 사람에게 신호가 간다.
- `quantity`는 계약 단위의 배수다. 단위가 안 되는 자투리는 배분하지 않고 재고로 남는다.
- `peg`: M4는 테스트에서 지정하고, M5부터는 production_plan이 정한다. 생산 중 물량(그 물량이 묶인 수주가 있음)은 `hard`다.
- `validation`은 `forecast_records`와 같은 형식이다. 새 결론을 쓸 때는 아직 검증받지 않았으므로 `validation`을 비운다.
  supply_coordination의 카드는 `produces`가 없어서(8번 절) `passed`가 되면 이 기록은 "완료" 상태이고(GRAPH_FLOW.md
  "신호 규칙") sales_channel이 이 값을 읽는다. 사람이 `commitments_exceed_capacity`를 처리해 넣은 하한 수량은 이 기록에 직접
  쓰지 않고 다음 조율의 입력이 되므로, 그 조율의 결론은 평소처럼 검증받는다.

### 4. `role_logs{}`
역할별 로그 `{role_tag: [entry]}`가 원본이다. 현재값 필드(`forecast_records`, `allocations`)가 판단에 쓰는 지금 상태라면,
로그는 **서로 다른 역할들의 이벤트가 시간순으로 쌓이는 기록**(DB의 트랜잭션 로그와 같은 역할)이다. Step1 원칙3
(판단용 현재값 vs 기록용 스냅샷 분리) 재적용: 현재값 필드=판단에 쓰는 현재 상태, 로그=기록(감사·역추적용).
(LangSmith와 별개 — 이건 agent 판단이 실제로 참조하는 데이터, LangSmith는 사람이 보는 디버깅용)
```
role_logs: { "{role_tag}": [ { seq, ts, role_tag, agent_id | null, event, round | null, payload } ] }
```
- **쓰기**: 각 역할은 자기 로그(`role_logs.{role_tag}`)에 **이어서 쓰기만** 한다(`role_permissions`의 `a`, 6번 절). 기존 항목은
  고칠 수 없다. 로그 쓰기 함수가 `seq`와 `ts`를 붙인다. `seq`는 모든 역할을 통틀어 하나로 늘어나는 순번이다.
- **`event`는 정해진 사건 이름만 쓴다.** 값(`agent_id`, `line_id`, 의심되는 원인, 판정 시각 등)은 `event` 문자열에 넣지
  않고 `agent_id` 필드와 `payload`에 담는다. 사건 이름: forecast는 `scenario_decided`, `scenario_not_computable`,
  `scenario_options_exhausted`, `rerun`, `forwarded`, `next_agent_unresolved`, `forecast_run_error`, `forecast_run_skipped`,
  `escalation_expired`, supply_coordination은 `allocation_decided`(조율 결론을 씀), `allocation_escalated`(라인을 사람에게
  올림), `commitment_gap_noticed`(약정 차이 알림), `escalation_expired`, `misrouted_send_back`(받은 기록을 자기 일이 아니라고
  되돌림), 검증agent는 `validation_passed`, `validation_failed`, `validation_error`, 사람은 `resolution_applied`다.
- **`payload`에는 역추적에 필요한 값을 담는다.** forecast 재실행(`rerun`)은 재실행의 계기(`trigger`: `failed_verdict` 불합격
  판정 또는 `send_back`), 덮어쓰기 전의 `assumptions`, `scenario`, `selection_basis`, `validation`과 의심되는 원인을 담고,
  판정(`validation_*`)은 `suspected_causes`와 `rationale`과 판정 시각(`validation_ts`)을 담는다. `forecast_run_error`는 실패한
  단계와 예외 내용을 담는다. `forwarded`는 `forward_to`와, 기록이 통과한 시각을 담는다. `next_agent_unresolved`는
  `reason`(`no_next_agent`/`multiple_next_agents`)과 후보와 제외한 agent를, `misrouted_send_back`은 되돌린 agent(`from_role`)와
  그 기록이 통과한 시각을 담는다. "통과한 시각"은 `passed` 판정으로 통과한 기록이면 판정 시각(`validation_ts`),
  검증 면제 통과한 기록이면 escalation 처리 시각(`resolution_ts` — 사람의 처리는 `resolution.ts`, 시한
  초과는 그 처리를 한 시각)이고, 둘 중 하나만 채운다(GRAPH_FLOW.md "받는 쪽 반송"). `allocation_decided`는 `line_id`, 조율의 종류(`kind`:
  정기 `regular`/이어지는 `continuing`), 조율에 넣은 입력(`inputs`, 변경은 이 입력과의 차이로 계산한다), 공급 가능량, 단계별 배분 결과(미납분·계약 하한·마진·비율), 마지막 순서로 얹은 값, 덮어쓰기 전의
  `allocations` 항목을 담는다. `resolution_applied`는 escalation 기록과 넣은 값(forecast 기록이면 덮어쓰기 전 `scenario` 포함)을, `escalation_expired`는 escalation 기록과 시한
  초과 처리 결과, 처리한 시각(`expired_at`)을 담는다.
- **`negotiation_log`는 저장하지 않는다.** `role_logs`의 항목을 `seq` 순으로 합쳐 읽는 함수의 결과이고, 역할 사이에서
  이벤트가 어떻게 겹치거나 어긋나는지 볼 때 쓴다. 이력을 읽는 곳은 모두 이 함수를 쓴다.
- **로그를 판단에 쓰는 곳**(예: 같은 기록에 `misrouted`로 되돌린 agent 찾기)은 `event` 문자열의 값을 해석하지 않고 사건 이름과
  `agent_id`와 `payload`의 필드로 조회한다.

검증 이벤트도 별도 필드 없이 여기에 함께 쌓임 — "통과"/"이상감지" 각각의 현재값은 해당 record
(`forecast_records[i].validation` 등)에 있고, 그 이력(언제 통과였다가 언제 번복됐는지)은 합쳐 읽은 로그를 시간순으로 읽으면 됨.

### 5. `interaction_protocol[]`
코드가 조회하는 엣지별 기준값(교환 상한, 같은 이유 반복 기준, 알림 기준, 처리 모드 등)을 담는다. 트레이싱·상호작용 기록이나
외부 벤치마크로 조정할 값이 있는 엣지만 두고, 값이 고정인 경우(항상 `intervention`이고 조정할 값이 없는 reason 등)는 두지
않는다. 엣지의 흐름은 GRAPH_FLOW.md에 있다. `scope`는 인스턴스 나열이 아니라 역할 태그다. 같은 엣지에 트리거가 여러 개일 수
있어 조회 키는 `(edge, escalation_trigger)`다.
```
{ edge, scope: [role_tag, ...],
  max_exchanges,                  # (선택) 한 라운드 안의 요청·응답 횟수 상한 — 타임아웃 안전장치
  repeat_escalation_threshold,    # (선택) 같은 이유가 이 횟수만큼 연속 반복되면 상한 소진을 기다리지 않고
                                  #        "구조적으로 안 풀림"으로 보고 상위로 확장
  escalation_trigger, escalation_target, escalation_kind: "rule" | "agent_judgment" | "human",
  escalation_mode: "intervention" | "notice",  # 멈추고 사람의 결정을 기다림 / 알리고 진행
  notice_threshold,               # (선택) notice를 보내는 기준값
  source: "initial_design" | "promoted_from_trace" | "external_benchmark",
  last_updated }
```
`max_exchanges`와 `repeat_escalation_threshold`는 M5의 회의(supply_coordination ↔ plan agent)에서 쓴다. 라운드와 요청·응답의 뜻은
GRAPH_FLOW.md "send-back·재실행 관련 용어"에 있다.

지금 항목은 셋이다.

**supply_coordination → human_manager, 약정 차이 알림**: 배분이 최소 구매 약정의 이번 주기 몫과 기준 이상 차이 나면 알리고
진행한다. 약정의 구속력에 따라 기준이 다르다(구속력이 없으면 우리가 손실을 떠안으므로 더 작은 차이에도 알림).
```
{ edge: "supply_coordination->human_manager", scope: ["supply_coordination", "human_manager"],
  escalation_trigger: "commitment_gap", escalation_target: "human_manager",
  escalation_kind: "rule", escalation_mode: "notice",
  notice_threshold: { binding: 0.2, non_binding: 0.05 },  # 약정의 이번 주기 몫과 배분의 차이 ÷ 약정의 이번 주기 몫
  source: "initial_design", last_updated }
```

**supply_coordination → human_manager, 계약 하한 초과**: 미납분과 계약 하한을 합쳐 그 라인의 공급 가능량을 넘으면 어느
계약을 줄일지 사람이 정한다. 모드를 트레이싱·처리 결과를 보고 바꿀 수 있게 항목을 둔다.
```
{ edge: "supply_coordination->human_manager", scope: ["supply_coordination", "human_manager"],
  escalation_trigger: "commitments_exceed_capacity", escalation_target: "human_manager",
  escalation_kind: "human", escalation_mode: "intervention",
  source: "initial_design", last_updated }
```

**forecast → human_manager, 가정 선택 ③**: 가정들의 값이 서로 많이 다르고, 과거 정확도로 매긴 점수에서도 1등 가정이
2등을 뚜렷하게 앞서지 못해 어느 가정을 믿을지 정할 수 없을 때 사람을 부르는 경우다(판단 기준은 AGENT_NODE_LIST.md forecast
5단계). 사람이 필요한지(모드)를 조정할 대상이라 항목을 둔다. ③의 수치 기준은 forecast 내부 판단 기준이라
`judgment_thresholds.py`에 둔다.
```
{ edge: "forecast->human_manager", scope: ["forecast", "human_manager"],
  escalation_trigger: "selection_unresolved", escalation_target: "human_manager",
  escalation_kind: "rule", escalation_mode: "intervention",
  source: "initial_design", last_updated }
```

**갱신 경로**:
- `promoted_from_trace`(내부): `negotiation_log`(이벤트 로그)를 process mining 라이브러리(pm4py 등)로 분석해 패턴(예: "이
  회의는 보통 N번의 요청·응답 안에 결론이 난다")을 발견 → `max_exchanges` 등 값 조정
- `external_benchmark`(외부): 업계 벤치마크 자료(유사 협상/거래의 평균 소요 등)를 초기값/조정 근거로 사용
- 둘 다 규칙(①) 계층의 기준값(`max_exchanges`, `notice_threshold`, `escalation_mode` 등)만 바꿈

### 6. `role_permissions[]`
State 필드 단위 접근권한(agent 간, Unity Catalog의 시스템 접근통제와는 다른 층). 화이트리스트 방식 — "전체 접근"은 기본값이
아니라 예외적으로만 명시.
```
{ role_tag, field_path, access: "r" | "w" | "a" }
```
- `r` 읽기, `w` 쓰기, `a` 이어서 쓰기다. `a`는 목록 끝에 새 항목을 더하는 것만 허용하고 기존 항목을 고치거나 지우는 쓰기는
  거부한다. 로그(`role_logs.{role_tag}`)는 그 역할에 `a`다.
- `field_path`의 인스턴스 인덱스는 와일드카드 `[*]`를 쓸 수 있다. 예: 검증agent `forecast_validation`은
  `forecast_records[*].validation`에만 쓸 수 있어 모든 인스턴스의 판정 기록만 쓰고 다른 필드는 못 쓴다. 인덱스가 없는
  `field_path`(`forecast_records`)는 모든 인덱스와 그 아래 필드를 포함한다.
- `capacity_pools`: `supply_coordination`(r, 배분 판단용), 검증agent `allocation_validation`(r, 공급 가능량 확인용), `production_plan`(r/w, 자기 라인 자원이므로, M5). procurement_plan·
  logistics_plan은 접근하지 않는다(2번 절).
- `allocations`: `supply_coordination`(w), 검증agent `allocation_validation`(`allocations[*].validation`만 w),
  `human_manager`(r). plan agent(r)는 M5.
- `agent_cards`: 카드를 가진 agent가 읽는다(r).
- `escalation_records`: 기록을 만드는 agent는 새 기록을 더한다(a). 사람의 처리 함수와 시한 초과 처리는 `status`와 `resolution`을
  쓴다(7번 절). 담당별 읽기 범위(업무 담당자는 자기 담당 기록만, `system_operator`는 전부)는 행 단위라 `role_permissions`가
  아니라 escalation 기록을 읽는 함수가 `target_role`로 거른다.

### 7. `escalation_records[]`
사람에게 올라간 escalation 기록. 사람은 둘이다 — `human_manager`(업무 판단)와 `system_operator`(시스템 오류). `mode`로 종류를
구분한다 — `intervention`은 진행을 멈추고 사람의 결정을 기다리는 escalation 기록, `notice`는 알리기만 하고 진행하는
escalation 기록(사람의 결정이 없으므로 `status`와 `resolution`이 없음).
```
{ agent_id | null,                       # escalation 기록이 발생한 인스턴스("{company_id}:{item_id}"). 인스턴스와 무관하면 null
  line_id | null,                        # supply_coordination이 라인 단위로 올린 기록이면 그 라인
  trigger_edge, reason, rationale,       # rationale: 사람이 읽는 이유 설명
  target_role: "human_manager" | "system_operator",
  mode: "intervention" | "notice",
  log_seq,                               # 이 escalation과 관련된 로그 항목의 seq(`role_logs`, 4번 절)
  deadline | null,                       # 응답 시한. 시한 초과 처리가 있는 reason만(아래 표)
  status: "open" | "resolved" | "expired" | null,   # intervention일 때만. notice는 null
  resolution: { resolved_by, ts, kind: "value" | "rerun" | "dismiss", value, note } | null }  # intervention일 때만
```
`log_seq`가 가리키는 로그 항목은 escalation 사유를 뒷받침하는 값을 담는다. `forecast_run_error`는 예외 내용이 담긴 로그 항목
(`forecast_run_error`)을, 그 밖의 경우는 escalation 시점의 값이 담긴 로그 항목을 가리킨다(예: `no_computable_assumption`은
제외된 가정과 이유, `selection_unresolved`는 그때의 가정 값과 시나리오, `options_exhausted`는 유지한 시나리오와 가정 목록,
`validation_error`는 검증 오류 내용, `commitments_exceed_capacity`는 그 라인의 수요·하한·공급 가능량). 보류에 들어갈 때 로그
항목, escalation 기록, 작성agent의 기록(있으면)을 한 번의 State 갱신으로 쓰고, 보류 중인 인스턴스는 실행하지 않으므로 그때의
값은 이 로그 항목에서 찾는다.

**reason 대응표**. 담당·긴급도·시한 초과 처리는 State 필드가 아니라 코드 안의 reason 대응표로 정한다(긴급도는 reason에서
정해지는 값이라 저장하지 않는다). 새 reason을 더할 때는 이 표와 코드의 대응표에 함께 정한다. `emergency`는 진행할 값이 없거나
시스템 장애인 경우, `warning`은 값은 있지만 확인이 필요한 경우다.

| reason | 만드는 agent | 담당(`target_role`) | mode | 긴급도 | 시한 초과 시 처리 |
|---|---|---|---|---|---|
| `no_computable_assumption`(계산된 가정이 하나도 없어 요청량을 만들 수 없음) | forecast | human_manager | intervention | emergency | 그 인스턴스의 예측 수요 없이 진행(이번 주기 배분에서 빠짐) |
| `selection_unresolved`(가정 선택 ③) | forecast | human_manager | intervention | warning | 규칙이 낸 중간값(`scenario` 그대로)으로 진행 |
| `options_exhausted`(재실행 전에 계산된 `scenario`가 있었는데 재실행으로 가정이 모두 제외됨. `scenario`와 `assumptions`는 재실행 전 값을 그대로 둠) | forecast | human_manager | intervention | warning | 재실행 전 값으로 진행 |
| `forecast_run_error`(forecast 실행이 예외로 끝남. 기록은 이전 값 그대로) | forecast | system_operator | intervention | emergency | 없음(처리될 때까지 시스템 대기) |
| `no_next_agent`(`passed` 또는 검증 면제 통과 뒤에 넘길 후보 agent가 없음) | forecast | system_operator | intervention | warning | 없음 |
| `multiple_next_agents`(후보가 여럿이라 고르지 못함. M7 전까지) | forecast | system_operator | intervention | warning | 없음 |
| `validation_error`(검증 절차가 비정상 종료함) | 검증agent | system_operator | intervention | emergency | 없음 |
| `commitments_exceed_capacity`(미납분과 계약 하한을 합쳐 라인의 공급 가능량을 넘음) | supply_coordination | human_manager | intervention(`interaction_protocol`에서 바꿀 수 있음) | emergency | 없음(처리될 때까지 그 라인은 이전 결론 유지) |
| `pegged_exceed_capacity`(강한 묶임 물량 합계가 공급 가능량을 넘음 — capacity가 줄어든 경우) | supply_coordination | human_manager | intervention | emergency | 없음(그 라인은 이전 결론 유지) |
| `allocation_invalid`(배분 결론이 검증 불합격인데 입력이 판정 때와 같음 — 배분 로직 오류) | supply_coordination | system_operator | intervention | emergency | 없음 |
| `commitment_gap`(배분과 약정의 이번 주기 몫의 차이가 알림 기준을 넘음) | supply_coordination | human_manager | notice | warning | 해당 없음 |

- `no_next_agent`와 `multiple_next_agents`는 기록의 값을 그대로 두고 `forward_to`는 비어 있다.
- 모든 `intervention`은 `status: "open"`으로 시작한다. 시한 초과 처리가 있는 reason은 만들 때 `deadline`을 정한다. 시한의 길이는
  설정값이고 기본값은 다음 정해진 조율 시각(하루 한 번, GRAPH_FLOW.md "조율 흐름")이다.
- `trigger_edge`: forecast의 reason은 `forecast->human_manager` 또는 `forecast->system_operator`, 검증agent는
  `{validator_role_tag}->system_operator`, supply_coordination은 `supply_coordination->human_manager` 또는
  `supply_coordination->system_operator`다. 검증agent의 기록은 `agent_id`가 검증 대상 인스턴스다.

**사람의 처리(`resolution`)**. 사람이 값을 쓰는 경로는 이것뿐이다.
- `human_manager`는 `kind: "value"`로 값을 넣는다. forecast의 reason은 요청량(숫자) 하나이고, `commitments_exceed_capacity`는
  고객사별 하한 수량(`{demand_ref: quantity}`), `pegged_exceed_capacity`는 유지할 강한 묶임 수량(`{demand_ref: quantity}`)이다. 라인의
  두 값은 기록에 직접 쓰지 않고 `resolution`에 남아 다음 조율의 입력이 된다.
- `system_operator`는 `kind: "rerun"`(실패한 실행을 다시 한다 — `forecast_run_error`는 forecast 실행, `validation_error`는 검증) 또는
  `kind: "dismiss"`(이번 주기에서 그 인스턴스를 뺌)를 넣는다.
- **입력 제한**: 값은 넣을 때 확인한다(0 이상, 계약 단위의 배수, 과거 범위를 크게 벗어나면 경고, 같은 입력의 중복 거부).
  agent 경계를 넘는 제약(공급 가능량 등)은 그 값이 배분으로 들어간 뒤 배분 결론의 검증에서 확인되므로 사람이 넣은 값은
  검증agent를 다시 거치지 않는다.
- **반영**: 처리 함수가 escalation 기록의 `status`(`resolved`)·`resolution`과 값을 받는 기록을 한 번의 `update_state`로 쓴다.
  forecast 기록이면 `scenario.value`에 요청량을, `selection_basis`에 `"human"`을 쓰고 `validation`은 비워 둔다(판정 칸에는
  검증agent의 판정만 쓴다). 기록이 "판정됨"이 되어(GRAPH_FLOW.md "신호 규칙") forecast가 다음 agent로 넘긴다.
- **라인 reason의 값 반영**: `commitments_exceed_capacity`의 값(`{demand_ref: quantity}`)은 `demand_ref`가 속한 (고객사, item)의 계약 하한을
  그 수량으로 대체한다(값에 없는 (고객사, item)의 하한은 계산된 대로). `pegged_exceed_capacity`의 값은 그 수요의 강한 묶임 수량을 값 이하로
  줄인다(남는 부분은 약한 묶임이 되고, 값에 없는 강한 묶임은 그대로). 두 값 모두 기록에 직접 쓰지 않고 처리된 escalation 기록의 `resolution`에
  남아 그 계획 주기의 이후 조율마다 입력에 들어간다(같은 `demand_ref`에 값이 여럿이면 나중에 처리한 값이 이긴다).
- **값을 넣는 모양**: forecast의 reason에서 `scenario`는 `value`가 넣은 요청량이고 `assumption_ids`는 빈 목록, `derivation`은 null이다(기존
  `scenario`가 있든 없든 같다). 덮어쓰기 전 `scenario`는 로그 `resolution_applied`의 `payload`에 남는다. 처리할 때 `forward_to`와 `send_back`도
  비운다(사람이 넣은 값이 이전 값을 대신하므로).
- **시스템 담당의 처리**: `rerun`은 `validation_error`이면 판정 칸을 비워 기록이 다시 "검증 대기"가 되게 하고, 그 밖의 reason은 처리 상태만
  바꾼다(기록의 상태가 다시 계산돼 담당 agent에게 신호가 간다). `dismiss`는 기록을 바꾸지 않는다.
- **시한 초과**: 시한이 되면 스케줄러가 기록을 만든 agent의 채널로 신호를 보내고, 그 agent는 기록이 아직 `open`이고 시한이
  지났으면 위 표의 처리를 한다. `status`를 `expired`로 쓴다. 값으로 진행하는 경우 반영 방식은 사람의 처리와 같고
  `selection_basis`는 `"expiry"`다(`validation`은 비워 둔다). 예측 수요 없이 진행하는 경우 `scenario`와 `selection_basis`는
  `null`로 남는다.

### 8. `agent_cards[]`
경로 위 agent(supply_coordination, forecast처럼 기록을 받고 넘기는 agent)마다 카드 하나. 다음 agent 선택(GRAPH_FLOW.md
"다음 agent 선택")과 받는 쪽 반송의 기준이다. 검증agent와 사람은 경로 밖이라 카드가 없다.
```
{ role_tag,
  description,                   # 이 agent가 하는 일(M7의 LLM이 후보 중에서 고를 때 읽음)
  accepts: string | null,        # 받는 것(넘겨받는 기록의 종류). 받지 않으면 null
  produces: string | null,       # 내는 것(넘기는 기록의 종류). 넘기지 않으면 null
  response_type: "optimization" | "handoff" | "round_accumulation",  # 넘겨받은 기록에 응답하는 방식
  known_agents: [role_tag] }     # 이 agent가 넘길 수 있는 agent
```
- `response_type`은 GRAPH_FLOW.md "상호작용 세 가지 유형"의 이름이다(`optimization` 최적화, `handoff` 핸드오프형,
  `round_accumulation` 라운드 누적형).
- 두 agent가 이어지는지는 보내는 쪽의 `known_agents`에 받는 쪽이 있고 받는 쪽의 `accepts`가 보내는 쪽의 `produces`와 같은지로 본다.
- `produces`는 실행 중에 계산되는 값이 아니라 설계 때 정하는 값이다. `produces`가 없는 agent의 기록은 `passed` 또는 검증 면제 통과가 되면
  넘길 곳이 없으므로 "완료"다(GRAPH_FLOW.md "신호 규칙").
- 지금 카드: `forecast`(`produces`: "검증을 통과한 수요 예측", `known_agents`: `[supply_coordination]`), `supply_coordination`
  (`accepts`: "검증을 통과한 수요 예측", `produces`: null, `known_agents`: `[]`). plan agent 카드와 supply_coordination의
  `produces`·`known_agents`는 M5에서 더한다. 검증 면제 통과한 forecast 기록도 같은 카드로 넘긴다.

### 9. `validation_settings[]`
어느 agent를 어느 검증agent가 언제 검증하는지를 담는다. 검증 시점의 기준은 하나다 — **agent가 맡은 일이 끝났을 때**. agent마다
맡은 일이 어디서 끝나는지가 다를 뿐이고, 이 표가 그것을 적어 둔다.
```
{ role_tag,                                  # 검증받는 agent
  validator_role_tag,                        # 그 agent를 검증하는 검증agent
  validation_point: "per_record" | "on_conclusion" }
```
- `per_record`: 기록을 쓸 때마다 검증한다(기록 하나가 일 한 번). `on_conclusion`: 회의(M5)에 쓴 기록은 검증하지 않고, 회의를
  끝내고 쓴 결론 기록(`allocations`)만 검증한다.
- 지금 행: `forecast` → `forecast_validation`(`per_record`), `supply_coordination` → `allocation_validation`(`per_record`). M4에는
  회의가 없어 supply_coordination의 조율 한 번이 `allocations` 기록 한 번이다. M5에서 supply_coordination은 `on_conclusion`으로
  바꾸고 plan agent 행을 더한다.
- 기록이 "검증 대기"인지와 신호를 받을 검증agent의 채널은 이 표에서 정한다(GRAPH_FLOW.md "신호 규칙"). 검증 면제 통과
  (`selection_basis`가 `human`/`expiry`)인 forecast 기록은 이 표에 따른 검증 대상이 아니다(1번 절).

## State 접근 규칙

- 모든 State 읽기/쓰기는 `role_permissions`를 검사하는 wrapper 함수(예:
  `get_field(role_tag, field_path)`, `set_field`, `update_state`)를 통해서만 한다 — 코드가
  다른 role의 영역을 직접 건드리는 걸 런타임에 막기 위함.
- 여러 곳을 함께 쓸 때(기록과 그 로그 항목, 판정과 로그 항목과 escalation 기록 등)는 `update_state`를 쓴다. 쓸 곳마다 쓰기 권한을
  먼저 확인하고, 모든 쓰기를 한 번의 State 갱신으로 반영한다(전부 쓰거나 하나도 쓰지 않는다). `set_field`는 필드 하나를 쓰는
  `update_state`다. 신호는 쓰는 쪽이 지정하지 않고 기록의 상태가 바뀔 때 자동으로 간다(GRAPH_FLOW.md "신호 규칙").
- `a` 권한인 경로에는 목록 끝에 더하는 쓰기만 허용한다. 기존 항목을 바꾸는 쓰기는 wrapper가 거부한다.
- `capacity_pools`처럼 여러 태스크가 동시에 건드릴 수 있는 값은
  `asyncio.Lock`으로 감싸 동시 수정(race condition)을 막는다.
- **설정 필드**: `interaction_protocol`(5번), `role_permissions`(6번), `agent_cards`(8번), `validation_settings`(9번)는 시스템을
  처음 설정할 때 넣고 agent는 읽기만 한다. `interaction_protocol`의 기준값만 갱신 경로(5번 절)로 바뀐다.

## 아직 정하지 않은 것

- 실행층이 실제 agent가 되면 `execution_records[]` 등 새 최상위 필드로 분리
  예정(GRAPH_FLOW.md 참고, 지금은 외부 경계라 해당 없음)
- **회의 기록 `meetings[]` (M5)**: supply_coordination과 plan agent가 함께 읽고 쓰는 회의 기록. 조율하는 동안 쌓이고 조율이
  끝나면 닫히며, 결론은 `allocations`에 쓴다(결론과 과정을 나눠 `allocations`는 항상 확정된 값만 담는다). 지금 정한 모양:
  ```
  { meeting_id, scope,                       # 회의 범위(문제가 생긴 자원을 쓰는 수요들)
    status: "open" | "concluded" | "escalated",
    entries: [
      { seq, author,                         # author는 쓰는 agent 자신이어야 한다
        reply_to: seq | null,                # 어느 기록에 대한 답인지
        request: {...} | null,               # 다른 agent에게 바라는 것
        content: {...},                      # 판단 내용(가능·불가·대안과 근거, 대안이면 아끼는 비용과 영향받는 수요)
        based_on: seq,                       # 이 기록을 쓸 때 읽은 마지막 기록 번호(낙관적 동시성 제어)
        validations: [ { status, suspected_causes, rationale, ts, validator_role_tag } ] } ] }
  ```
  `entries`는 참여 agent에게 `a`, `entries[*].validations`는 그 영역의 검증agent에게만 `a`다. 끝나는 조건과 동시 쓰기 규칙은
  M5에서 먼저 설계하고 확정한다(GRAPH_FLOW.md "공급망계획agent 연결 구조").
- **공급 보장 물량 미충족 시 escalation과 sales_channel "조정 불가" 통지의 순서**: escalation이 먼저 사람에게 조치 기회를 준 뒤에만
  sales_channel로 "조정 불가"가 나가야 함(순서가 반대면 사람이 풀 수 있었던 건이 이미 불가로 통지됨). M5에서 구현.
- **(재확인 필요) capacity_pools 집계 수준의 적절성**: supply_coordination이 보는 총량/잔여 수준이 production_plan의 세부
  스케줄(changeover 등)에 비해 너무 거칠어 회의가 반복될 위험 — M5에서 negotiation_log 반복 패턴으로 실증 확인.
- **라인과 풀이 1:1이 아니게 되는 경우**(같은 라인 여러 개를 한 풀로 묶거나, 한 라인의 생산능력 일부를 특정 계약용으로 떼어
  두는 경우): 지금은 둘 다 없다. 필요가 생기면 라인을 묶는 풀을 따로 둔다.
