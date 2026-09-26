# Step 2 그래프 흐름 설계

State/노드 목록이 "무엇이 있는지"였다면, 이 문서는 "그것들이 어떤 순서·
조건으로 연결되는지"를 정리한다(agent 간 연결·신호·종료조건·동시성).
동시성 모델은 asyncio(단일 프로세스, 아래 "동시성 모델" 참고) — 아래 "엣지"는 그래프의 정적 연결이
아니라 **태스크 간 신호(asyncio.Queue) 교환**으로 구현된다. 이 모델에서는
"모든 회사의 응답이 도착해야 다음으로 넘어간다"는 제약이 없음 —
supply_coordination 태스크는 그때그때 도착한 만큼만 보고 판단 가능.

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
없고, push 여부는 받는 agent 성격(워커풀=pull, 조율=push)에 따른
기계적 규칙일 뿐이다(아래 "검증agent" 참고). human_manager(들) —
escalation 발생 시 반응(알림은 받기만 함), 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 되돌릴지는 interaction_protocol이
규정.
```

## 동시성 모델 (asyncio)

- **구조**: forecast((회사,item) 인스턴스별, 총 N개)와 supply_coordination/
  공급망계획agent(각 1개)를 각각 독립된 **asyncio 태스크**로 실행 — Docker/
  Redis 같은 별도 프로세스·네트워크 없이, 파이썬 프로세스 하나 안에서
  이벤트루프가 태스크들을 오가며 진행(cooperative multitasking)
- **왜 LangGraph Send API가 아닌지**: Send API는 "한 스텝 안에서 N개로
  갈라졌다가 그 스텝이 끝나야 합쳐지는" 모델이라, "A회사 처리 중 응답을
  기다리는 동안 B회사 작업을 진행"하는 독립적 병행이 안 됨. asyncio
  태스크는 `asyncio.create_task()`로 만들고, I/O 대기(LLM 응답 대기 등)
  중엔 다른 태스크가 자동으로 진행됨. "라운드"는 그래프 스텝이 아니라
  신호 교환으로 구현된다
- **통신**: agent 간 직접 호출이 아니라 항상 State를 거침 — 한쪽이 State에
  값을 쓰고 in-process 신호(`asyncio.Queue`)로 알리면, 상대가 그 신호를 받아
  State를 읽고 반응. 큐는 "초인종" 역할만 하고 기록은 State/negotiation_log가
  담당. State 접근 wrapper와 Lock은 STATE_SCHEMA.md "State 접근 규칙" 참고
- **한계**: 진행 중인 LLM 호출 하나에 끼어들 수는 없음 — LLM 호출 자체가
  원자적 단위
- **확장 여지(지금 안 만듦)**: State를 거쳐서만 통신하는 원칙을 지키면,
  나중에 in-process 큐를 Redis 등 외부 큐로 교체해 물리적으로 분리된 서버와
  통신하게 확장 가능 — agent 로직은 안 건드리고 큐 구현체만 교체

### 계획 주기와 스냅샷

실행 리듬은 실시간 연속 스트림이 아니라 **계획 주기**(월간 등) 기반이다.
- 주기가 시작될 때 그 시점까지의 데이터로 **스냅샷을 고정**하고, 주기
  동안(되돌림 재실행 포함) 모든 agent는 이 스냅샷만 본다
- 주기 중 발생한 실제 주문은 실행층(sales_channel)의 일이며 다음 주기
  스냅샷에 반영된다
- POS 공유 지연은 스냅샷 기준일보다 앞선 데이터까지만 포함하는 식으로
  표현한다
- 고정 파일에서 기준일까지를 잘라 읽으므로, 테스트에서는 기준일을 옮겨
  여러 주기를 재현할 수 있다

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

## 검증agent

**검증agent는 판정 권한만 갖고, 라우팅 권한은 갖지 않는다.** "값이 문제
없는지"는 검증agent가 판단하지만, "문제가 생겼을 때 그걸 어디로 되돌릴지"는
이미 각 agent 자신의 기존 역할에 있는 권한이다 — forecast의 핸드오프
되돌림, supply_coordination의 "공급망계획agent 문제 신호 처리" 판단 등.
검증agent가 라우팅까지 대신하면 이 권한이 중복된다.

흐름:
1. 값을 쓴 agent는 **원래 의도한 다음 agent 큐가 아니라, 검증agent 큐에만
   push**(변경 없음)
2. 검증agent(워커풀, 무기억)가 pull해서 확인 — 판단3계층 적용:
   - **①규칙**: 대상 agent의 계산을 **재현하지 않고**,
     독립적인 제약조건만 확인(예: 배분 합계가 `capacity_pools` 총량을
     넘는가, `role_tag`가 실제 그 필드 쓰기 권한이 있는가). 계산을
     재현하면 항상 "통과"만 나오는 죽은 검증이 됨 — 반드시 피해야 함
   - **②agent판단**: "이 근거가 지금 상황에서 타당한가" 같은
     애매한 판단만 LLM으로
3. 검증agent는 **판정 결과(`validation.status`)만 State에 쓴다** — push
   여부는 판정 결과와 받는 agent 성격에 따라 갈린다(워커풀 성격이 받는
   `passed`는 push 없음 — 값 쓰기와 push가 항상 짝이라는 기존 원칙의
   예외). 판정별 후속:
   - **`passed`**: push 여부가 **값을 받는 agent의 성격**에 따라 갈린다 —
     "판정만 하고 라우팅은 안 한다"는 원칙과는 별개로(어디로 보낼지
     정하는 게 아니라, 이미 정해진 목적지에 "지금 넘길지 말지"만 정하는
     기계적 규칙이라 라우팅 판단이 아니다):
     - **받는 쪽이 워커풀 성격**(트리거되면 반응 — 검증agent 자신,
       `procurement_plan`/`production_plan`/`logistics_plan`처럼 여러
       회사 요청을 안건 단위로 처리하는 agent들): 검증agent는 **push하지
       않는다**(값 쓰기와 push가 항상 짝이라는 기존 원칙의 예외). 이런
       agent는 이미 자기가 처리 중인 안건 단위로 State를 스스로 확인하러
       가므로(pull), 검증agent가 대신 넘겨줄 필요가 없다. 예:
       `supply_coordination`이 `procurement_plan`에게 보낸 요청이
       `passed`면, `procurement_plan`이 스스로 확인해서 처리.
     - **받는 쪽이 조율 성격**(계속 능동적으로 여러 요청을 처리·판단하는
       agent — `supply_coordination`): 검증agent가 **push**한다(원래
       의도했던 목적지 채널로 — `flagged`가 쓰는 `validation_result.
       {role_tag}`와는 다른, 그 값의 정상적인 수신 채널). `supply_coordination`은
       우선순위 계산·"공급망계획agent 문제 신호 처리" 같은 다른
       판단을 계속 수행 중이라, 검증 결과 확인을 스스로 챙기게 하면 그
       판단 업무에 부담이 됨 — 그래서 검증agent가 대신 알려준다. (`procurement_plan` 등
       워커풀 성격 agent에는 이 이유가 해당 안 됨 — 안건 단위 확인이
       애초에 이들의 본래 일이라 확인 자체가 부담이 아니다.) 예:
       `forecast`가 선택한 시나리오가 `passed`면 `supply_coordination`
       에게 push, `procurement_plan`의 응답이 `passed`면
       `supply_coordination`에게 push.
     - 각 agent가 어느 쪽인지는 AGENT_NODE_LIST.md의 agent별 설명(몇 개
       회사/안건을 동시에 처리하는지)을 따른다.
   - **`flagged`**: 검증agent가 레코드를 만든 작성agent(문제 원인
     제공자)의 `validation_result.{role_tag}` 채널로 **push**한다 —
     pull만으로는 작성agent가 "자기 값에 문제가 생겼는지"를 미리 알 수
     없어 깨어나지 못하기 때문(그래서 이 경우만 push가 필요). **검증agent는
     "누구에게 문제가 있는지"만 알리고, "그 문제를 어디로 되돌릴지"는
     판단하지 않는다** — 이후 처리(자체 재조정할지, 더 위로 되돌릴지)는
     작성agent 자신의 기존 역할이 판단한다(검증agent가 그래프 구조 전체를
     알아야 하는 상황을 피하기 위함). **같은 `routing_reason`(또는 거부
     사유)이 연속 K회 반복되면 "이 agent 선에서 구조적으로 안 풀림"으로
     간주해 `max_rounds` 소진을 기다리지 않고 상위로 확장**하는 판단도
     작성agent 쪽의 몫 — K는 `interaction_protocol`의
     `repeat_escalation_threshold`(edge별 기준값 하나, 예: 3)이고, 실제
     "몇 번 반복됐는지"는 별도로 저장하지 않음 — 그 edge의
     `exchanges`/`round_history`를 최근 것부터 훑어 같은 사유가 연속
     몇 개인지 그때그때 계산. **이 카운트는 edge+사유 단위로만 유효** —
     다른 edge로 넘어가면(예: 상위로 확장돼 다른 agent가 처리) 그 agent의
     기록에서 새로 계산되므로 자동으로 리셋됨(누적 이월 없음)
   - **`check_failed`**(검증 절차 자체가 비정상 종료 — 판단 문제가 아니라
     시스템 장애): 라우팅 판단이 아니라 장애 처리라 성격이 달라 기존
     그대로 유지 — 검증agent가 몇 차례 자체 재시도 → 그래도 안 되면
     escalation 큐로 **push**("시스템 장애" 사유, "판단 이상"과 구분).
     **다음 agent는 이 상태의 레코드를 받지 않음**(워커풀 성격이면
     pull 대상에서, 조율 성격이면 push 대상에서 제외 — 검증 미해결
     상태로 방치되지 않도록 어느 경로로도 전달되지 않음)

`flagged`, `check_failed`, 그리고 조율 성격 agent가 받는 `passed`처럼
검증agent가 실제로 push하는 경우에 한해, 그 push를 받는 지속 태스크는
관련 채널을 지켜봐야 함 — `flagged`/`check_failed`는 **자기
`validation_result.{role_tag}` 채널**(문제가 생겼다는 알림, 새로 추가되는
구독 대상), `passed`는 그 값의 **원래 정상적인 수신 채널**(기존에 이미
지켜보고 있던 채널)이라 별도 구독이 필요 없다.

**여전히 남는 한계**: 검증agent 자신이 "이상 없음(passed)"을 잘못 낸
경우는 실시간으로 못 잡음 — "검증을 검증하는" 상위 검증을 또 두면 같은
문제가 무한히 반복되기 때문(의도적 트레이드오프). 다운스트림에서 실제 값이
그 통과 기록과 어긋나야(예: 다음 라운드 실적이나 실제 배송 결과와 크게
벗어남) 사후적으로만 발견되고, 발견 즉시 자동 재처리하지 않고
`escalation_records`로 사람에게 간다(이 시점엔 어느 validation agent가 왜
틀렸는지 자체를 신뢰할 수 없으므로). `flagged`/`check_failed`는 능동적으로
알리므로 이 한계에서 제외된다.

## 상호작용 세 가지 유형

- **핸드오프형** (supply_coordination→forecast, 역방향·예외): 같은 값을
  다듬는 게 아니라 **재실행 지시** — 되돌림을 받으면 이전 결과를 이어서
  다듬는 게 아니라 새로 계산해서 덮어씀(현재값만 유지, 이력은
  negotiation_log). 공급망계획agent가 infeasible을 보냈을 때만 열리는
  예외 경로 — 평소엔 아예 열리지 않는다. infeasible은 "숫자를 조금씩
  좁혀가며 밀당"할 대상이 아니라 "선택이 틀렸을 수 있으니 다시 하라"는
  신호이므로, 새 라운드 메커니즘을 만들지 않고 forecast agent 자신의
  내부 재실행 메커니즘을 그대로 재사용한다. `suspected_cause`에 따른 재개
  지점과 대응은 AGENT_NODE_LIST.md forecast agent "되돌림" 참고.
- **최적화** (forecast→supply_coordination 정방향, 평소 경로): forecast가
  선택한 시나리오(`selected_scenario`)의 예측값을 supply_coordination이 우선순위 점수 산출 후
  `allocation_candidate`로 생성하는 **단방향 전달** — 라운드가 쌓이지
  않는다. 상대의 응답을 받아 값을 조정하는 절차가 아니라 한 번의 계산으로
  끝나므로 "협상"이 아니다. 다만 이건 **협상 응답(값 조정)이 없다는
  뜻일 뿐**, 검증을 아예 안 거친다는 뜻은 아니다 — 검증agent의 `flagged`
  되돌림 경로(아래 "검증agent" 참고)는 다른 모든 엣지와 동일하게 이
  엣지에도 적용된다.
- **라운드 누적형(협상)** (supply_coordination↔공급망계획agent들):
  `round_history`/`exchanges` 배열에 **누적** — 이전 라운드를 지우지 않고
  옆에 쌓으며 제안을 조금씩 조정. `request`/`response`는 자유 객체라
  단순 가부가 아니라 역제안(대안 조건)을 담을 수 있음 — "협상"과 "일방
  통보(예: 검증)"를 가르는 지점. 요청 내용은 supply_coordination의
  최적화 계산 결과지만, 이 엣지 자체는 처음부터 라운드 누적형이다 —
  요청 이후 `feasible`/`infeasible` 응답을 반드시 받아야 하고,
  `feasible`이면 1라운드 만에 즉시 종료되며, `infeasible`일 때만 라운드가
  연장된다.

같은 agent 쌍(forecast/supply_coordination) 사이에도 방향에 따라 유형이
갈릴 수 있다 — 평소엔 정방향 최적화만 흐르고, 공급망계획agent의 infeasible
신호로 supply_coordination이 되돌림을 시작했을 때만 역방향 핸드오프
(재실행 지시)가 열린다(엣지 표 참고).

세 유형 모두 검증agent의 판정을 받는다 — `flagged`면 작성agent에게
되돌아가고, `check_failed`면 다음 소비자가 그 레코드를 걸러낸다(위
"검증agent" 참고).

## 공급망계획agent 연결 구조 — hub-and-spoke, 사슬(chain) 아님

supply_coordination은 procurement_plan·production_plan·logistics_plan agent
셋 모두와 **개별적으로 직접** 연결된다. "조달→생산→배송"은 한 라운드
안에서 의존관계에 따른 **호출 순서**일 뿐, 구조적 강제가 아니다 — 그래서:
- 배송계획에서 문제 발생 시 생산계획을 거치지 않고 조달계획이나
  supply_coordination으로 바로 되돌아갈 수 있음
- 이번 요청에 생산 단계가 필요 없으면 건너뛸 수 있음
  (`allocation_candidates[i].required_stages`로 명시)

## 엣지 표

| edge | 유형 | 반복 여부 | 종료조건 | escalation 대상 |
|---|---|---|---|---|
| forecast → supply_coordination (정방향, 평소) | 단방향 전달(최적화) | 아니오 | forecast가 선택한 시나리오(`selected_scenario`)의 예측값을 우선순위 점수 산출 후 allocation_candidate로 생성 | 없음 |
| supply_coordination → forecast (역방향, 예외) | 핸드오프형 — 공급망계획agent(procurement_plan 등)가 infeasible을 보냈을 때만 열림 | 아니오 | 재실행 완료(재개 지점부터) | 없음(forecast 자신의 시나리오 선택 판단3계층 — ③ 사람 escalation 포함 — 에 위임) |
| supply_coordination ↔ procurement_plan | 라운드 누적형 | 예 | `response_status: feasible` | max_rounds 소진 → 사람, 또는 공급망조율 판단으로 forecast/채널까지 재확장 |
| supply_coordination ↔ production_plan | 라운드 누적형 | 예 | 위와 동일 | 위와 동일 |
| supply_coordination ↔ logistics_plan | 라운드 누적형 | 예 | 위와 동일 | 위와 동일 |
| supply_coordination → sales_channel | 단방향(출력) | 아니오 | 즉시(배분 결정 반영) | 없음 |
| sales_channel → forecast | 단방향(입력) | 아니오 | 즉시(주문 이력·POS·프로모션 일정·계약 조건 유입, 주기 스냅샷 기준) | 없음 |
| human_input → supply_coordination | 단방향(입력) — forecast를 거치지 않는 수량 | 아니오 | 즉시(배분 대상에 포함) | 없음 |
| forecast → human_manager | 단방향(알림) — 진행을 멈추지 않음 | 아니오 | 즉시(알림 전달) | 없음 |
| 작성 agent → validation agent(들) | 판정(라우팅 권한 없음) | 아니오 | `passed`/`flagged`/`check_failed` 판정 | check_failed 반복 시 사람(시스템 장애 사유) |
| escalation_trigger → human_manager | 단방향 | 아니오 | 사람의 resolution 입력 | (최종 단계) |

공급망계획agent 간 직접 상호작용(procurement_plan↔production_plan 등)은
표에서 제외 — 아직 미정, 지금은 반드시 supply_coordination을 경유.
`exchanges`에 쌓이는 `routing_reason`(STATE_SCHEMA.md 참고)이 이 미정
상태를 나중에 풀 근거가 됨 — pm4py가 negotiation_log에서 "이 유형은 항상
공급망조율의 추가 판단 없이 그냥 전달되더라"는 패턴을 찾으면 직접 연결
edge로 승격.

## 인스턴스 패턴 — 언제 배열+agent_id, 언제 아닌지

새로운 "복수 인스턴스" 역할이 생길 때마다 매번 다시 고민하지 않도록,
기준을 정리:

- **지속되는 정체성이 있는 안건**(forecast_records처럼 "A회사"에 계속
  매임) → **배열 + agent_id**, 각자 자기 정체성에 매인 값을 유지 — 라운드가
  실제로 쌓이는 필드(`exchanges` 등)가 있으면 이력까지, forecast_records처럼
  핸드오프형이면 현재값만(이력은 negotiation_log)
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
