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
다음 agent에게 전달하지 않는다. human_manager(들) —
escalation 발생 시 반응(알림은 받기만 함), 지속 태스크 아님.

문제 발생 시: 공급망계획agent → supply_coordination → (필요시) forecast/
채널/사람 escalation — 어디까지 send-back 또는 escalation할지는 interaction_protocol이 규정.
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
  값을 써서 기록의 상태가 바뀌면 담당에게 in-process 신호(`asyncio.Queue`)가 가고(아래 "신호 규칙"),
  상대가 그 신호를 받아 State를 읽고 반응. 큐는 "초인종" 역할만 하고 기록은 State/role_logs가
  담당. State 접근 wrapper와 Lock은 STATE_SCHEMA.md "State 접근 규칙" 참고
- **한계**: 진행 중인 LLM 호출 하나에 끼어들 수는 없음 — LLM 호출 자체가 원자적 단위
- **확장 여지(지금 안 만듦)**: State를 거쳐서만 통신하는 원칙을 지키면,
  나중에 in-process 큐를 Redis 등 외부 큐로 교체해 물리적으로 분리된 서버와
  통신하게 확장 가능 — agent 로직은 안 건드리고 큐 구현체만 교체

### 계획 주기와 스냅샷

실행 리듬은 실시간 연속 스트림이 아니라 **계획 주기**(월간 등) 기반이다.
- 주기가 시작될 때 그 시점까지의 데이터로 **스냅샷을 고정**하고, 주기
  동안(재실행 포함) 모든 agent는 이 스냅샷만 본다
- 주기 중 발생한 실제 주문은 실행층(sales_channel)의 일이며 다음 주기 스냅샷에 반영된다
- POS 공유 지연은 스냅샷 기준일보다 앞선 데이터까지만 포함하는 식으로 표현한다
- 고정 파일에서 기준일까지를 잘라 읽으므로, 테스트에서는 기준일을 옮겨 여러 주기를 재현할 수 있다

## Push/Pull 용어 정리

이 문서에서 "신호"는 Git의 push/pull이 아니라 **누가 행동을 시작하는가**를 가리킨다:
- **Push**: 값을 쓴 쪽이 `queue.put()`으로 능동적으로 신호를 보냄
- **Pull**: 받는 쪽이 스스로 큐를 지켜보다 `queue.get()`으로 가져감(워커가
  여럿이면 그중 준비된 하나만 가져감 — "work queue / competing consumers"
  패턴, 방송(broadcast)이 아님)

### 신호 규칙

신호는 기록의 상태를 기준으로 보낸다. 값을 쓴다고 신호가 저절로 가지 않는다.

- **채널**: agent마다 채널은 하나이고 이름은 그 agent의 `role_tag`다(검증agent는 검증agent의 role_tag, 사람은 `human_manager`).
  신호에는 기록의 위치(`record_path`)가 담기지만 어떤 일을 할지는 담기지 않는다. 받는 쪽은 기록의 상태를 읽어 할 일을 정한다.
- **기록의 상태**는 이미 있는 값에서 계산하며 따로 저장하지 않는다. 상태는 다섯 가지이고, 여러 조건에 해당하면 표의 위쪽 상태가
  우선한다. 아무것도 쓰지 않은 기록(값 `scenario`가 없고 아래 조건에도 해당하지 않음)은 상태가 없다.

  | 우선순위 | 상태 | 조건 | 담당 (신호가 가는 채널) |
  |---|---|---|---|
  | 1 | 사람 대기 | 그 인스턴스에 처리되지 않은 `intervention` escalation 기록이 있음 | `human_manager` |
  | 2 | 되돌려짐 | `send_back`이 있음 | 그 기록의 작성agent의 `role_tag` |
  | 3 | 전달됨 | 판정이 `passed`이고 `forward_to`가 있음 | `forward_to`의 agent의 `role_tag` |
  | 4 | 판정됨 | 판정(`validation.status`: `passed`/`failed`/`error`)이 있음 | 그 기록의 작성agent의 `role_tag` |
  | 5 | 검증 대기 | 값이 있고 판정이 비어 있음 | 검증agent의 `role_tag` |

- **보내는 쪽**: State 갱신으로 기록의 상태가 바뀌면 그 상태의 담당 채널로 자동으로 신호를 보낸다. 상태를 바꾸지 않는 쓰기(로그
  항목 등)는 신호를 보내지 않는다. 쓰는 쪽이 신호를 따로 지정하지 않는다.
- **받는 쪽**: 신호를 받으면 기록을 직접 읽고, 지금 상태가 자기가 맡은 상태일 때만 일한다(전달됨은 `forward_to`가 자기일 때). 같은
  채널로 오는 상태가 여럿이면(작성agent는 판정됨과 되돌려짐) 상태를 읽어 할 일을 가른다. 자기 담당이 아니면 넘어간다(늦게 도착한
  신호 포함).
- **넘김**: `passed` 뒤에 다음 agent로 보내는 일은 작성agent가 다음 agent를 골라 `forward_to`에 쓰는 것이다(아래 "다음 agent 선택").
  기록이 "전달됨"이 되어 그 agent에게 신호가 가고, 이미 "전달됨"인 기록은 다시 넘기지 않는다(중복 전달 방지는 상태로 판단한다).
  `forward_to`에는 작성agent의 카드 `known_agents`에 있는 agent만 쓸 수 있고 그 밖의 agent는 쓰기가 거부된다.
- **되돌림**: 기록을 받은 agent(또는 그 뒤 단계의 agent)가 기록을 쓴 agent에게 되돌리려고 `send_back`을 쓴다. 기록이 "되돌려짐"이 되어
  작성agent에게 신호가 간다. 값 문제(`misrouted`가 아닌 이유)이면 작성agent가 재실행한 결과를 쓸 때 `validation`, `forward_to`,
  `send_back`이 비어 기록이 "검증 대기"로 돌아간다. `misrouted`이면 재실행하지 않는다(아래 "받는 쪽 반송").

### 다음 agent 선택

agent 카드(STATE_SCHEMA.md 8번 절)가 경로 위 agent마다 하는 일(`description`), 받는 것(`accepts`), 내는 것(`produces`), 넘길 수 있는
agent(`known_agents`)를 담는다. 검증agent와 human_manager는 경로 밖이라 카드가 없다.

1. 작성agent는 `passed`를 확인한 뒤 자기 카드의 `known_agents` 중 그 카드의 `accepts`가 자기 `produces`와 맞는 agent를 후보로 고른다.
2. 후보가 하나면 그 agent를 `forward_to`에 쓴다. 후보가 없으면 escalation(`no_next_agent`), 여럿이면 escalation(`multiple_next_agents`)이다
   (둘 다 `intervention`이라 기록이 "사람 대기"가 된다). 여럿일 때 고르는 판단은 M7에서 LLM이 카드의 `description`을 읽고 하며, 그 전에는
   규칙 스텁이 고른다(MILESTONES.md).

### 받는 쪽 반송

"전달됨" 상태의 기록을 받은 agent가 내용을 보고 자기 일이 아니라고 판단하면(자기 카드의 `accepts`와 맞지 않음) `send_back`에
`suspected_causes`의 `type: "misrouted"`(문제 내용 없음)를 써서 되돌린다. 작성agent는 `misrouted`를 받으면 재실행하지 않고 다음 agent
선택만 다시 한다. 같은 기록(같은 판정)에 대해 `misrouted`로 반송한 agent는 후보에서 뺀다. 반송 이력은 반송한 agent의 로그에서 찾는다.
다시 고른 결과가 새 `forward_to`이면 `send_back`을 비우고 새로 넘긴다. 남은 후보가 없으면 `no_next_agent` escalation이다.

## send-back·재실행 관련 용어

이 절이 아래 용어의 유일한 정의다. AGENT_NODE_LIST.md와 STATE_SCHEMA.md는 이 절을 참조만 한다.

| 용어 | 정의 |
|---|---|
| **send-back** | 경로 위 agent 사이에서 기록을 받은 쪽이 그 기록을 쓴 agent(작성agent)에게 돌려보내는 것(기록의 `send_back`). 의심되는 원인의 목록(`suspected_causes`)을 싣는다. 원인은 값 문제(공급망계획agent의 infeasible 때문에 supply_coordination이 forecast·채널로 보내는 경우)이거나 `misrouted`(자기 일이 아님)다. 받는 쪽은 항상 그 기록의 작성agent다. 검증agent의 판정은 send-back이 아니다. |
| **불합격 판정** | 검증agent가 기록의 `validation`에 `failed`와 `suspected_causes`를 쓰는 것. 검증agent는 경로 밖이라 되돌리는 것이 아니라 판정을 기록하고, 작성agent가 기록을 읽고 처리한다. |
| **의심되는 원인** | 결과에 문제가 있다고 보는 이유 하나(`suspected_causes`의 항목, 형식은 STATE_SCHEMA.md). 불합격 판정(`validation`)과 send-back(`send_back`)이 모두 이 항목의 목록을 싣는다. |
| **재실행(re-run)** | send-back(`misrouted` 제외)이나 불합격 판정을 받은 작성agent가 자기 내부 단계를 다시 도는 것. agent 내부의 일이다. 같은 입력이면 같은 결과가 나오므로 의심되는 원인을 입력에 넣어 입력을 바꾼 채 돈다. 구체 정의는 지금 forecast만 있고(AGENT_NODE_LIST.md), 다른 agent는 해당 마일스톤에서 정한다. |
| **재개 지점** | 재실행이 시작되는 agent 내부 단계(의심되는 원인으로 정해진다). |
| **라운드** | supply_coordination↔공급망계획agent 사이 요청·응답 한 번(`exchanges`/`round_history`). 라운드 연장과 `max_rounds`는 이 엣지에만 있다. |
| **재조정** | supply_coordination이 자체로 푸는 것. |
| **상위로 확장** | agent 사이에서 같은 이유가 `repeat_escalation_threshold`번 연속 반복되면 `max_rounds` 소진을 기다리지 않고 위/옆 agent 또는 사람으로 넘기는 것. **재실행(agent 내부)에는 쓰지 않는다.** |
| **제외** | 가정이나 소스를 쓰지 않는 것(`excluded_assumptions`, `excluded_sources`). |
| **소진** | 두 가지이고 섞지 않는다. `max_rounds` 소진(라운드 수 상한)과 `options_exhausted`(forecast 재실행으로 가정이 모두 제외됨). |
| **escalation** | 사람(human_manager)에게 올리는 것(`intervention`/`notice`). |

## 검증agent

검증agent가 무엇을 보는지, 판정만 하고 라우팅하지 않는 원칙과 이유, 판단 방식은 AGENT_NODE_LIST.md 검증agent 절이 정의한다.
이 절은 누가 누구에게 무엇을 보내는지의 흐름만 정리한다.

흐름:
1. 작성agent는 자기 일이 모두 끝난 뒤 기록을 한 번 쓴다(모든 작성agent에 적용한다. 일을 하는 동안 기록에 중간 결과를
   쓰지 않는다). 그 쓰기로 기록이 "검증 대기" 상태가 되면 **원래 의도한 다음 agent가 아니라 검증agent에게만** 신호가 간다.
   보류로 끝난 일(처리되지 않은 `intervention` escalation 기록이 있는 인스턴스)은 기록이 "사람 대기" 상태가 되므로
   검증agent에게 신호가 가지 않는다.
2. 검증agent(워커풀)는 신호를 받으면 기록을 읽어 "검증 대기"일 때만 판정한다. 판정(`validation.status`)과 그 로그 항목(`error`면
   escalation 기록도)을 한 번의 State 갱신으로 쓰면 기록이 "판정됨"(`error`면 "사람 대기") 상태가 되어 작성agent의
   채널(`error`면 `human_manager`)로 신호가 간다. 다음 agent에게는 전달하지 않는다.
3. 판정별로 다음에 어디로 가는지:
   - **`passed`**: 작성agent가 신호를 받아 기록이 "판정됨"인지 읽고, 다음 agent를 골라 `forward_to`에 써서 **직접** 넘긴다(위 "다음
     agent 선택"). 기록이 "전달됨"이 되어 그 agent에게 신호가 간다. 예: `forecast`가 `supply_coordination`에게.
   - **`failed`**(불합격 판정): 작성agent가 처리한다(처리 판단은 AGENT_NODE_LIST.md 검증agent 절). 같은 `routing_reason`(또는 거부
     이유)이 연속 K회 반복되면 "이 agent 선에서 구조적으로 안 풀림"으로 간주해 `max_rounds` 소진을 기다리지 않고
     **상위로 확장**한다. 이 판단은 작성agent 쪽의 몫이며 **라운드 누적형 엣지에서만** 쓴다(forecast의 재실행은 스스로
     끝나므로 failed의 반복을 셀 필요가 없다). K는 `interaction_protocol`의 `repeat_escalation_threshold`
     (STATE_SCHEMA.md)이고, "몇 번 반복됐는지"는 별도로 저장하지 않고 그 edge의 `exchanges`/`round_history`를 최근
     것부터 훑어 같은 이유가 연속 몇 개인지 그때그때 계산한다. **이 카운트는 edge+이유 단위로만 유효**하다 — 다른
     edge로 넘어가면(예: 상위로 확장돼 다른 agent가 처리) 그 agent의 기록에서 새로 계산되므로 자동으로 리셋된다 (누적 이월 없음).
   - **`error`**(검증 절차 자체가 비정상 종료 — 시스템 장애): 재시도 없이 바로
     `escalation_records`에 escalation 기록을 만든다(기록이 "사람 대기"가 되어 human_manager에게 신호가 간다). 기록의 필드
     값은 STATE_SCHEMA.md 7번 절을 따른다. **그 인스턴스만 멈추고 다른 인스턴스는 진행한다.** 작성agent는 이 판정을 받아도 할 일이 없고, **다음
     agent는 이 상태의 레코드를 받지 않는다**(작성agent는 `passed`인 기록만 보낸다).

### 사람 입력과 검증의 무결성

모든 엣지에 적용하는 공통 규칙이다.
- 처리되지 않은 `intervention` escalation 기록이 있는 인스턴스의 기록은 "사람 대기" 상태이므로 검증agent도 다음 agent도 그 기록에
  일하지 않는다(받는 쪽이 상태를 확인한다).
- 그 인스턴스의 작성agent는 첫 실행을 포함해 실행하지 않고, 건너뛴 사실만 자기 로그에 남긴다(기록도 escalation 기록도 새로 만들지
  않는다). 다음 계획 주기에서 보류를 어떻게 할지는 아직 정하지 않았다.
- 사람이 기록에 값을 쓰는 경로는 `intervention`의 `resolution`뿐이다. 그 밖의 경로로 사람이 기록을 고치지 않는다.

## 상호작용 세 가지 유형

- **핸드오프형** (기록을 받은 agent가 자기 일이 아니거나 값에 문제가 있다고 작성agent에게 send-back을 보내는 유형. 값 문제는
  supply_coordination이 forecast에 보내는 역방향·예외 경로): 같은 값을
  다듬는 게 아니라 **send-back** — 받은 쪽이 기록의 `send_back`을 쓰면 기록이 "되돌려짐"이 되어
  작성agent에게 신호가 가고, 값 문제이면 forecast는 이전 결과를 이어서
  다듬는 게 아니라 새로 계산해서 덮어씀(현재값만 유지, 이력은
  `role_logs`). 공급망계획agent가 infeasible을 보냈을 때만 열리는
  예외 경로 — 평소엔 아예 열리지 않는다. infeasible은 "숫자를 조금씩
  좁혀가며 밀당"할 대상이 아니라 "선택이 틀렸을 수 있으니 다시 하라"는
  신호이므로, 새 라운드 메커니즘을 만들지 않고 forecast agent 자신의
  내부 재실행 메커니즘을 그대로 재사용한다. `suspected_cause`에 따른 재개
  지점과 대응은 AGENT_NODE_LIST.md forecast agent "재실행" 참고.
- **최적화** (forecast→supply_coordination 정방향, 평소 경로): forecast가
  가정 선택이 정한 최종 요청량(`scenario`)을 supply_coordination이 우선순위 점수 산출 후
  `allocation_candidate`로 생성하는 **단방향 전달** — 라운드가 쌓이지
  않는다. 상대의 응답을 받아 값을 조정하는 절차가 아니라 한 번의 계산으로
  끝나므로 "협상"이 아니다. 다만 이건 **협상 응답(값 조정)이 없다는
  뜻일 뿐**, 검증을 아예 안 거친다는 뜻은 아니다 — 검증agent의 불합격 판정
  경로(아래 "검증agent" 참고)는 다른 모든 엣지와 동일하게 이
  엣지에도 적용된다.
- **라운드 누적형(협상)** (supply_coordination↔공급망계획agent들):
  `round_history`/`exchanges` 배열에 **누적** — 이전 라운드를 지우지 않고
  옆에 쌓으며 제안을 조금씩 조정. `request`/`response`는 자유 객체라
  단순 가부가 아니라 역제안(대안 조건)을 담을 수 있음 — "협상"과 "일방
  통보(예: 검증)"를 가르는 지점. 요청 내용은 supply_coordination의
  최적화 계산 결과지만, 이 엣지 자체는 처음부터 라운드 누적형이다 —
  요청 이후 `feasible`/`infeasible` 응답을 반드시 받아야 하고,
  `feasible`이면 1라운드 만에 즉시 종료되며, `infeasible`일 때만 라운드가 연장된다.

같은 agent 쌍(forecast/supply_coordination) 사이에도 방향에 따라 유형이
갈릴 수 있다 — 평소엔 정방향 최적화만 흐르고, 공급망계획agent의 infeasible
신호로 supply_coordination이 forecast에 send-back을 보낼 때만 역방향 핸드오프형이 쓰인다(엣지 표 참고).

세 유형 모두 검증agent의 판정을 받는다 — `failed`(불합격 판정)면 작성agent가
처리하고, `error`면 다음 소비자가 그 레코드를 걸러낸다(위 "검증agent" 참고).

## 공급망계획agent 연결 구조 — hub-and-spoke, 사슬(chain) 아님

supply_coordination은 procurement_plan·production_plan·logistics_plan agent
셋 모두와 **개별적으로 직접** 연결된다. "조달→생산→배송"은 한 라운드
안에서 의존관계에 따른 **호출 순서**일 뿐, 구조적 강제가 아니다 — 그래서:
- 배송계획에서 문제 발생 시 생산계획을 거치지 않고 조달계획이나 supply_coordination으로 바로 되돌아갈 수 있음
- 이번 요청에 생산 단계가 필요 없으면 건너뛸 수 있음 (`allocation_candidates[i].required_stages`로 명시)

## 엣지 표

경로 위 agent 사이에 넘길 수 있는지의 원본은 각 agent 카드의 `known_agents`다. 아래 표의 경로 위 agent 행은 연결의 성격(유형, 반복,
종료조건, escalation 대상)을 보여주는 요약이고, `known_agents`에 있는 쌍(되돌림의 경우 작성agent로 돌아가는 쌍)만 적는다. 받는 쪽이
응답하는 방식은 카드의 `response_type`(`optimization` 최적화, `handoff` 핸드오프형, `round_accumulation` 라운드 누적형), 보내는 쪽의
한도(`max_rounds` 등)는 `interaction_protocol`이 원본이다. 외부 경계·검증agent·사람 행은 카드와 무관하다.

| edge | 유형 | 반복 여부 | 종료조건 | escalation 대상 |
|---|---|---|---|---|
| forecast → supply_coordination (정방향, 평소) | 단방향 전달(최적화) | 아니오 | forecast가 정한 최종 요청량(`scenario`)을 우선순위 점수 산출 후 allocation_candidate로 생성 | 없음 |
| supply_coordination → forecast (역방향, 예외) | 핸드오프형 — 공급망계획agent(procurement_plan 등)가 infeasible을 보냈을 때만 supply_coordination이 forecast에 send-back을 보냄 | 아니오 | 재실행 완료(재개 지점부터) | 없음(forecast 자신의 가정 선택 판단3계층 — ③ 사람 escalation 포함 — 에 위임) |
| supply_coordination → sales_channel | 단방향(출력) | 아니오 | 즉시(배분 결정 반영) | 없음 |
| sales_channel → forecast | 단방향(입력) | 아니오 | 즉시(주문 이력·POS·프로모션 일정 유입, 주기 스냅샷 기준) | 없음 |
| human_input → supply_coordination | 단방향(입력) — forecast를 거치지 않는 수량 | 아니오 | 즉시(배분 대상에 포함) | 없음 |
| sales_channel → supply_coordination | 단방향(입력) — 최소 구매 약정·공급 보장 물량·MOQ | 아니오 | 즉시(주기 스냅샷 기준, 처리 규칙은 M4·M5) | 없음 |
| supply_coordination → human_manager | 단방향(알림) — 최소 구매 약정과 배분의 차이, 진행을 멈추지 않음 | 아니오 | 즉시(알림 전달, 구현은 M4·M5) | 없음 |
| 작성 agent → validation agent(들) | 판정(라우팅 권한 없음) | 아니오 | `passed`/`failed`/`error` 판정 | error면 바로 사람(`validation_error`) |
| escalation_trigger → human_manager | 단방향 | 아니오 | 사람의 resolution 입력 | (최종 단계) |

supply_coordination↔procurement_plan·production_plan·logistics_plan 행(라운드 누적형, 반복 있음, 종료조건 `response_status: feasible`,
max_rounds 소진 시 사람 또는 공급망조율 판단으로 forecast/채널까지 재확장)은 plan agent 카드와 supply_coordination의 `known_agents`가
추가되는 M5에서 표에 더한다.

공급망계획agent 간 직접 상호작용(procurement_plan↔production_plan 등)은
표에서 제외 — 아직 미정, 지금은 반드시 supply_coordination을 경유.
`exchanges`에 쌓이는 `routing_reason`(STATE_SCHEMA.md 참고)이 이 미정
상태를 나중에 풀 근거가 됨 — pm4py가 negotiation_log에서 "이 유형은 항상
공급망조율의 추가 판단 없이 그냥 전달되더라"는 패턴을 찾으면 직접 연결 edge로 승격.

## 인스턴스 패턴 — 언제 배열+agent_id, 언제 아닌지

새로운 "복수 인스턴스" 역할이 생길 때마다 매번 다시 고민하지 않도록, 기준을 정리:

- **지속되는 정체성이 있는 안건**(forecast_records처럼 "A회사"에 계속
  매임) → **배열 + agent_id**, 각자 자기 정체성에 매인 값을 유지 — 라운드가
  실제로 쌓이는 필드(`exchanges` 등)가 있으면 이력까지, forecast_records처럼
  핸드오프형이면 현재값만(이력은 `role_logs`)
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
