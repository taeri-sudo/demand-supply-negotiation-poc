# Step 2 그래프 흐름 설계

State/노드 목록이 "무엇이 있는지"였다면, 이 문서는 "그것들이 어떤 순서·
조건으로 연결되는지"를 정리한다. 동시성 모델은 asyncio(단일 프로세스,
STATE_DRAFT.md "동시성 모델" 참고) — 아래 "엣지"는 그래프의 정적 연결이
아니라 **태스크 간 신호(asyncio.Queue) 교환**으로 구현된다. 이 모델에서는
"모든 회사의 응답이 도착해야 다음으로 넘어간다"는 제약이 없음 —
supply_coordination 태스크는 그때그때 도착한 만큼만 보고 판단 가능.

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

## Push/Pull 용어 정리

이 문서에서 "신호"는 Git의 push/pull이 아니라 **누가 행동을 시작하는가**를
가리킨다:
- **Push**: 값을 쓴 쪽이 `queue.put()`으로 능동적으로 신호를 보냄
- **Pull**: 받는 쪽이 스스로 큐를 지켜보다 `queue.get()`으로 가져감(워커가
  여럿이면 그중 준비된 하나만 가져감 — "work queue / competing consumers"
  패턴, 방송(broadcast)이 아님)

**"값 쓰기"와 "신호 push"는 항상 짝**이다 — State에 값만 쓰고 큐에 안
넣으면 아무도 안 깨어난다. 값을 쓰는 wrapper 함수(`set_field`)가 값 기록과
동시에 해당 큐에 push하도록 구현해야 함(업무 로직이 매번 기억할 필요 없게).

## 검증 게이트 (validation gate)

**검증은 "옆에서 관찰"이 아니라 값이 다음 소비자에게 가기 전 반드시 거치는
관문이다** — 검증 없이 다음 단계가 먼저 진행되면, 나중에 이상이 발견됐을 때
되돌리는 비용(이미 진행된 작업 롤백, negotiation_log 오염, 이미 escalation된
상태와 뒤늦은 되돌림이 겹치는 복잡성)이 더 크다고 판단해 확정.

흐름:
1. 값을 쓴 agent는 **원래 의도한 다음 agent 큐가 아니라, 검증agent 큐에만
   push**
2. 검증agent(워커풀, 무기억)가 pull해서 확인 — 판단3계층 적용:
   - **①규칙(빠름, 대부분)**: 대상 agent의 계산을 **재현하지 않고**,
     독립적인 제약조건만 확인(예: 배분 합계가 `capacity_pools` 총량을
     넘는가, `role_tag`가 실제 그 필드 쓰기 권한이 있는가). 계산을
     재현하면 항상 "통과"만 나오는 죽은 검증이 됨 — 반드시 피해야 함
   - **②agent판단(느림, 드묾)**: "이 근거가 지금 상황에서 타당한가" 같은
     애매한 판단만 LLM으로
   - `validation.status`: `"passed"` | `"flagged"` | `"check_failed"`
3. 결과에 따라 검증agent가 **직접 다음 큐에 push**:
   - **`passed`**: 원래 의도했던 다음 agent 큐로 push (정상 진행)
   - **`flagged`**: 레코드를 만든 agent(문제 원인 제공자)의
     `validation_result.{role_tag}` 큐로 push — 그 agent가 판단3계층으로
     재조정 여부 결정. **같은 `routing_reason`(또는 거부 사유)이 연속 K회
     반복되면 "이 agent 선에서 구조적으로 안 풀림"으로 간주해 `max_rounds`
     소진을 기다리지 않고 상위로 확장**. K는 `interaction_protocol`의
     `repeat_escalation_threshold`(edge별 기준값 하나, 예: 3)이고, 실제
     "몇 번 반복됐는지"는 별도로 저장하지 않음 — 그 edge의
     `exchanges`/`round_history`를 최근 것부터 훑어 같은 사유가 연속
     몇 개인지 그때그때 계산. **이 카운트는 edge+사유 단위로만 유효** —
     다른 edge로 넘어가면(예: 상위로 확장돼 다른 agent가 처리) 그 agent의
     기록에서 새로 계산되므로 자동으로 리셋됨(누적 이월 없음)
   - **`check_failed`**(검증 절차 자체가 비정상 종료 — 판단 문제가 아니라
     시스템 장애): 검증agent가 몇 차례 자체 재시도 → 그래도 안 되면
     escalation 큐로 push("시스템 장애" 사유, "판단 이상"과 구분). **원래
     다음 agent는 이 상태의 레코드를 pull하지 않음**(검증 미해결 상태로
     방치되지 않도록 대상에서 제외)

각 지속 태스크는 기존 협상 채널에 더해 **자기 `validation_result.{role_tag}`
채널도 함께 지켜봐야** 함(새로 추가되는 구독 대상).

**여전히 남는 한계**: 게이트로 막아도 검증agent 자신이 "이상 없음(passed)"을
잘못 낸 경우는 실시간으로 못 잡음 — 이건 STATE_DRAFT.md "구조적 한계"에
남긴 대로, 다운스트림 불일치로 사후 발견되는 것 외에 방법이 없음(검증을
검증하는 무한회귀를 피하기 위한 의도적 트레이드오프).

## 상호작용 두 가지 유형

- **핸드오프형** (analysis↔forecast만 해당): 같은 값을 다듬는 게 아니라
  **재실행 지시** — forecast가 되돌리면 analysis는 이전 결과를 이어서
  다듬는 게 아니라 새로 계산해서 덮어씀(현재값만 유지, 이력은
  negotiation_log). 되돌릴 때 `suspected_cause`에 따라 재개 지점이 다름:
  - "모델 선택이 문제" → 모델 선택 단계부터만 재실행(수집된 데이터는 재사용)
  - "데이터 소스 자체가 부적합"(자사 이력으로 부족) → 데이터 수집부터 재실행
- **라운드 누적형** (forecast↔supply_coordination,
  supply_coordination↔공급망계획agent들): `round_history`/`exchanges` 배열에
  **누적** — 이전 라운드를 지우지 않고 옆에 쌓으며 제안을 조금씩 조정.
  `request`/`response`는 자유 객체라 단순 가부가 아니라 역제안(대안 조건)을
  담을 수 있음 — "협상"과 "일방 통보(예: 검증)"를 가르는 지점.

두 유형 모두 위 검증 게이트를 거친 뒤에야 상대에게 값이 전달된다.

## 엣지 표

| edge | 유형 | 반복 여부 | 종료조건 | escalation 대상 |
|---|---|---|---|---|
| analysis ↔ forecast | 핸드오프형 | 아니오 | 재실행 완료(재개 지점부터) | 없음(같은 클러스터 내 이동) |
| forecast ↔ supply_coordination | 라운드 누적형 | 예 | 변화폭 임계치 이하 **+** forecast_reliability 신뢰도 게이트 통과 | max_rounds 소진 → 사람 |
| supply_coordination ↔ procurement_plan | 라운드 누적형 | 예 | `response_status: feasible` | max_rounds 소진 → 사람, 또는 공급망조율 판단으로 forecast/analysis/채널까지 재확장 |
| supply_coordination ↔ production_plan | 라운드 누적형 | 예 | 위와 동일 | 위와 동일 |
| supply_coordination ↔ logistics_plan | 라운드 누적형 | 예 | 위와 동일 | 위와 동일 |
| supply_coordination → sales_channel | 단방향(출력) | 아니오 | 즉시(배분 결정 반영) | 없음 |
| sales_channel → analysis | 단방향(입력) | 아니오 | 즉시(실적 데이터 유입) | 없음 |
| 작성 agent → validation agent(들) | **게이트**(critical path) | 아니오 | `passed`/`flagged`/`check_failed` 판정 | check_failed 반복 시 사람(시스템 장애 사유) |
| escalation_trigger → human_manager | 단방향 | 아니오 | 사람의 resolution 입력 | (최종 단계) |

공급망계획agent 간 직접 상호작용(procurement_plan↔production_plan 등)은
표에서 제외 — 아직 미정, 지금은 반드시 supply_coordination을 경유.
`exchanges`에 쌓이는 `routing_reason`(STATE_DRAFT.md 참고)이 이 미정
상태를 나중에 풀 근거가 됨 — pm4py가 negotiation_log에서 "이 유형은 항상
공급망조율의 추가 판단 없이 그냥 전달되더라"는 패턴을 찾으면 직접 연결
edge로 승격.

## 인스턴스 패턴 — 언제 배열+agent_id, 언제 아닌지

새로운 "복수 인스턴스" 역할이 생길 때마다 매번 다시 고민하지 않도록,
기준을 정리:

- **지속되는 정체성이 있는 안건**(forecast_agents처럼 "A회사"에 계속
  매임) → **배열 + agent_id**, 각자 자기 이력(`round_history` 등)을 쌓음
- **워커풀처럼 아무나 다음 안건을 집어가는 경우**(validation agent들,
  또는 조달담당자가 여러 명으로 쪼개지는 미래 시나리오) → **별도 배열
  불필요** — 이미 안건 기준으로 존재하는 필드(`exchanges[i]` 등)에
  누가 처리했는지는 `handled_by`(선택적 부가 필드)로만 남김. 워커의
  정체성 자체가 기록의 핵심이 아니기 때문.

## 실행층이 실제 agent가 되는 경우 (미래)

지금 `exchanges[i].response_status`/`response`는 "커밋 가능 여부에 대한
최종 답변 하나"라 협상 데이터 안에 있어도 무방하지만, 나중에 실행 추적
agent(예: 실시간 배송 상태 — 출발/이동중/지연/도착)가 생기면 이건 **다른
계층의 사건**(Step1 원칙1)이라 `exchanges` 안이 아니라 **새 최상위 필드
(`execution_records[]` 등)**로 분리하고, `plan_id`/`exchanges[i]`를
참조(포함이 아니라 링크)하는 구조로 가야 함 — `capacity_pools`를 역할태그로
연결하고 임베딩 안 한 것과 같은 패턴. 지금은 실행층이 agent가 아니라
외부 경계라 해당 없음.
