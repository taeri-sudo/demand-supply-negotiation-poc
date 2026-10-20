# Step 2 그래프 흐름 설계

State/노드 목록이 "무엇이 있는지"였다면, 이 문서는 "그것들이 어떤 순서·
조건으로 연결되는지"를 정리한다(agent 간 연결·신호·종료조건·동시성).
동시성 모델은 asyncio(단일 프로세스, 아래 "동시성 모델" 참고) — 아래 "엣지"는 그래프의 정적 연결이
아니라 **태스크 간 신호(asyncio.Queue) 교환**으로 구현된다. 이 모델에서는
"모든 회사의 응답이 도착해야 다음으로 넘어간다"는 제약이 없음 —
supply_coordination은 생산라인마다 그 라인의 인스턴스만 기다리고(아래 "조율 흐름"), 다른 라인은 따로 진행한다.

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
- **신호 전달(확인 응답)**: 받는 쪽이 신호를 꺼내도 큐에서 바로 지우지 않고 "처리 중"으로 둔다. 일을 끝내면(자기 상태가 아니라
  그냥 넘어간 경우 포함) 처리했다고 알려 지운다. 정해진 시간 안에 확인이 없으면(처리 도중 실패) 그 신호만 다시 나타난다. 모든
  채널에 같은 규칙이다(사람 채널은 대시보드가 알림을 받으면 확인한다). 받는 쪽은 기록의 상태를 직접 읽고 판단하므로 같은 신호가
  다시 와도 결과가 같다. 외부 큐로 바꿀 때도 대부분의 외부 큐가 이 방식을 기본으로 제공한다
- **스케줄러**: 정해진 시각의 조율(아래 "조율 흐름")과 escalation 시한(STATE_SCHEMA.md 7번 절)은 스케줄러 태스크가 담당 채널로
  신호를 보낸다. 받는 쪽은 다른 신호와 똑같이 상태를 읽고 할 일이 있을 때만 일한다
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

## 조율 흐름 (supply_coordination)

조율은 생산라인(`capacity_pools`의 풀) 단위로 한다. 배분 규칙으로 비교하는 대상이 같은 라인의 수요뿐이라 다른 라인은 기다리지
않는다. 수요가 어느 라인에 속하는지는 라인 기준 정보(`line_assignments`)에서 찾는다. 배분 규칙은 AGENT_NODE_LIST.md
supply_coordination agent에 있다.

| 용어 | 뜻 |
|---|---|
| 정기 조율 | 계획 주기가 시작된 뒤 라인마다 한 번 하는 조율. 그 라인의 예측 수요·수주·사람이 정한 수량 전부로 배분한다 |
| 이어지는 조율 | 정기 조율 뒤 들어온 값으로 하는 조율. 남은 공급과 같은 라인의 약한 묶임 물량만 다시 나눈다 |
| 조율 완료 | supply_coordination이 조율 한 번을 끝내고 그 라인의 결론을 `allocations`에 쓴 상태 |

**정기 조율을 시작하는 때**(기본 규칙 — 교체 가능한 class로 둔다):
- 그 라인의 forecast 인스턴스 중 **처리 중인 것이 없으면** 시작한다. 처리 중 = 상태 없음(아직 실행 안 함), 검증 대기, 판정됨,
  되돌려짐(아래 "신호 규칙"의 상태). 재실행 중인 인스턴스는 처리 중이므로 재실행 결과는 같은 조율에 들어간다.
- **기다리지 않는 상태**: 전달됨(끝남), 업무 대기, 시스템 대기, 그리고 시한 초과로 예측 수요 없이 끝난 인스턴스. 업무 대기·시스템
  대기 인스턴스는 기다리지 않고 몫도 비워 두지 않는다. 처리되면 이어지는 조율로 들어온다.
- 정기 조율을 마치기 전에 그 라인에 온 변경 신호에는 아무것도 하지 않는다(정기 조율이 모두 함께 넣는다). 정기 조율을 마쳤는지는
  그 라인 `allocations`의 `cycle`이 이번 주기인지로 계산한다(따로 저장하지 않음). 정기 조율을 마치면 모여 있는 변경이 있는지 보고
  있으면 바로 이어지는 조율을 연다.

**입력 고정과 마지막 순서**:
- 조율은 시작할 때 입력(예측 수요, 수주, 사람이 정한 수량, 재고·생산 중 물량, capacity)을 고정하고 그 값으로만 끝까지 계산한다.
  진행 중인 조율에는 끼어들지 않는다.
- 결론을 쓰기 직전에 **한 번** 확인해서, 조율 중에 들어온 그 라인의 변경을 **전부** 마지막 순서로 얹는다(이어지는 조율과 같은 방식).
  그다음 결론을 한 번 쓴다. 확인을 반복하지 않으므로 얹는 동안 들어온 값은 바로 이어지는 조율로 넘어간다.

**이어지는 조율의 시점**:

| 들어오는 것 | 조율 시점 |
|---|---|
| 사람의 처리 결과(escalation `resolution`), 사람이 정한 수량 | 진행 중인 조율이 끝나는 즉시 |
| 자동으로 들어오는 변경 중 **급한 변경** — 납기가 변경 동결 구간(설정값, 기본 7일) 안이거나 미납분·계약 하한에 영향을 줌 | 진행 중인 조율이 끝나는 즉시 |
| 그 밖의 자동 변경(새 수주 등) | 모아 두었다가 하루 한 번 정해진 시각(설정값) |

- 조율을 열 때는 그 라인에 모여 있던 변경을 **전부** 함께 넣는다. 다른 라인에 모인 변경은 그 라인의 시점을 기다린다.
- 즉시 조율할 값은 들어올 때 신호를 보내고, 모으는 값은 스케줄러가 정해진 시각에 신호를 보낸다. 같은 라인의 신호가 여러 개면
  첫 신호를 처리할 때 모두 반영되고, 뒤 신호는 새로 할 일이 없어 넘어간다.
- 큐는 먼저 들어온 신호부터 꺼낸다(FIFO). 조율 안의 배분은 들어온 순서와 상관없이 배분 규칙으로 정하므로 같은 입력이면 같은
  결과가 나온다.

## Push/Pull 용어 정리

이 문서에서 "신호"는 Git의 push/pull이 아니라 **누가 행동을 시작하는가**를 가리킨다:
- **Push**: 값을 쓴 쪽이 `queue.put()`으로 능동적으로 신호를 보냄
- **Pull**: 받는 쪽이 스스로 큐를 지켜보다 `queue.get()`으로 가져감(워커가
  여럿이면 그중 준비된 하나만 가져감 — "work queue / competing consumers"
  패턴, 방송(broadcast)이 아님)

### 신호 규칙

신호는 기록의 상태를 기준으로 보낸다. 값을 쓴다고 신호가 저절로 가지 않는다.

- **채널**: agent마다 채널은 하나이고 이름은 그 agent의 `role_tag`다(검증agent는 검증agent의 role_tag, 사람은 `human_manager`와
  `system_operator`). 신호에는 기록의 위치(`record_path`)가 담기지만 어떤 일을 할지는 담기지 않는다. 받는 쪽은 기록의 상태를 읽어
  할 일을 정한다.
- **검증 면제 통과**(`exempt_passed`): 검증agent를 거치지 않고 통과한 forecast 기록이다. `selection_basis`가 `human`(사람이
  escalation을 처리해 넣은 값) 또는 `expiry`(시한이 지나 정해진 처리로 진행한 값)이면 해당한다(아래 "사람 입력과 검증의 무결성").
  판정 칸(`validation`)에는 검증agent의 판정만 쓰므로 이 기록의 판정 칸은 비어 있다. 왜 면제됐는지는 `selection_basis`가 구분하고,
  검증 면제 통과는 "검증agent가 검증했는가"만 가른다. 그 뒤 재실행 결과는 `selection_basis`를 새로 쓰므로 다시 검증 대상이 된다.
  `allocations`에는 `selection_basis`가 없어 해당하지 않는다(판정이 `passed`일 때만 넘긴다).
- 아래에서 "`passed` 또는 검증 면제 통과"는 기록을 다음 agent에게 넘기거나 "완료"로 둘 수 있는 조건이다.
- **기록의 상태**는 이미 있는 값에서 계산하며 따로 저장하지 않는다. 상태는 일곱 가지이고, 여러 조건에 해당하면 표의 위쪽 상태가
  우선한다. 아무것도 쓰지 않은 기록(값이 없고 아래 조건에도 해당하지 않음)은 상태가 없다. 기록은 `forecast_records`와
  `allocations`이고, "인스턴스"는 forecast 기록이면 그 인스턴스, `allocations`이면 그 라인이다.

  | 우선순위 | 상태 | 조건 | 담당 (신호가 가는 채널) |
  |---|---|---|---|
  | 1 | 업무 대기 | 그 인스턴스에 처리되지 않은 `human_manager` 담당 `intervention` escalation 기록이 있음 | `human_manager` |
  | 2 | 시스템 대기 | 그 인스턴스에 처리되지 않은 `system_operator` 담당 escalation 기록이 있음 | `system_operator` |
  | 3 | 되돌려짐 | `send_back`이 있음 | 그 기록의 작성agent의 `role_tag` |
  | 4 | 전달됨 | 판정이 `passed` 또는 검증 면제 통과이고 `forward_to`가 있음 | `forward_to`의 agent의 `role_tag` |
  | 5 | 완료 | 판정이 `passed` 또는 검증 면제 통과이고 작성agent의 카드에 `produces`가 없음 | 없음(신호를 보내지 않는다. sales_channel이 읽어 간다) |
  | 6 | 판정됨 | 판정(`validation.status`: `passed`/`failed`/`error`)이 있음, 또는 검증 면제 통과 | 그 기록의 작성agent의 `role_tag` |
  | 7 | 검증 대기 | 값이 있고 판정이 비어 있으며 검증 면제 통과가 아니고 `validation_settings`상 검증 대상임 | `validation_settings`의 검증agent `role_tag` |

- **보내는 쪽**: State 갱신으로 기록의 상태가 바뀌면 그 상태의 담당 채널로 자동으로 신호를 보낸다. 상태를 바꾸지 않는 쓰기(로그
  항목 등)는 신호를 보내지 않는다. 쓰는 쪽이 신호를 따로 지정하지 않는다.
- **받는 쪽**: 신호를 받으면 기록을 직접 읽고, 지금 상태가 자기가 맡은 상태일 때만 일한다(전달됨은 `forward_to`가 자기일 때). 같은
  채널로 오는 상태가 여럿이면(작성agent는 판정됨과 되돌려짐) 상태를 읽어 할 일을 가른다. 자기 담당이 아니면 넘어간다(늦게 도착한
  신호 포함).
- **넘김**: `passed` 또는 검증 면제 통과인 기록을 다음 agent로 보내는 일은 작성agent가 다음 agent를 골라 `forward_to`에 쓰는 것이다(아래 "다음 agent
  선택"). 기록이 "전달됨"이 되어 그 agent에게 신호가 가고, 이미 "전달됨"인 기록은 다시 넘기지 않는다(중복 전달 방지는 상태로
  판단한다). `forward_to`에는 작성agent의 카드 `known_agents`에 있는 agent만 쓸 수 있고 그 밖의 agent는 쓰기가 거부된다. 카드에
  `produces`가 없는 agent의 기록은 넘길 곳이 없어 `passed` 또는 검증 면제 통과가 되면 "완료"다.
- **되돌림**: 기록을 받은 agent(또는 그 뒤 단계의 agent)가 기록을 쓴 agent에게 되돌리려고 `send_back`을 쓴다. 기록이 "되돌려짐"이 되어
  작성agent에게 신호가 간다. 값 문제(`misrouted`가 아닌 이유)이면 작성agent가 재실행한 결과를 쓸 때 `validation`, `forward_to`,
  `send_back`이 비고 `selection_basis`가 새로 쓰여 기록이 "검증 대기"로 돌아간다. `misrouted`이면 재실행하지 않는다(아래 "받는 쪽 반송").

### 다음 agent 선택

agent 카드(STATE_SCHEMA.md 8번 절)가 경로 위 agent마다 하는 일(`description`), 받는 것(`accepts`), 내는 것(`produces`), 넘길 수 있는
agent(`known_agents`)를 담는다. 검증agent와 human_manager는 경로 밖이라 카드가 없다.

1. 작성agent는 기록이 `passed` 또는 검증 면제 통과(위 "신호 규칙")인지 확인한 뒤 자기 카드의 `known_agents` 중 그 카드의 `accepts`가 자기 `produces`와
   맞는 agent를 후보로 고른다.
2. 후보가 하나면 그 agent를 `forward_to`에 쓴다. 후보가 없으면 escalation(`no_next_agent`), 여럿이면 escalation(`multiple_next_agents`)이다
   (둘 다 카드 설정 문제라 `system_operator` 담당 `intervention`이고 기록이 "시스템 대기"가 된다). 여럿일 때 고르는 판단은 M7에서 LLM이 카드의 `description`을 읽고 하며, 그 전에는
   규칙 스텁이 고른다(MILESTONES.md).

### 받는 쪽 반송

"전달됨" 상태의 기록을 받은 agent가 내용을 보고 자기 일이 아니라고 판단하면(자기 카드의 `accepts`와 맞지 않음) `send_back`에
`suspected_causes`의 `type: "misrouted"`(문제 내용 없음)를 써서 되돌린다. 작성agent는 `misrouted`를 받으면 재실행하지 않고 다음 agent
선택만 다시 한다. 같은 기록에 대해 `misrouted`로 반송한 agent는 후보에서 뺀다. 반송 이력은 반송한 agent의 로그에서 찾는다.
- "같은 기록"은 같은 판정 또는 같은 escalation 처리로 통과한 기록이다. `passed` 판정으로 통과한 기록은 판정 시각
  (`validation_ts`)으로, 검증 면제 통과한 기록은 처리 시각(`resolution_ts`)으로 알아본다. 두 시각은 다른 이름으로
  따로 기록하고(STATE_SCHEMA.md 4번 절), 지금 기록과 같은 종류끼리만 비교한다.

다시 고른 결과가 새 `forward_to`이면 `send_back`을 비우고 새로 넘긴다. 남은 후보가 없으면 `no_next_agent` escalation이다.

## send-back·재실행 관련 용어

이 절이 아래 용어의 유일한 정의다. AGENT_NODE_LIST.md와 STATE_SCHEMA.md는 이 절을 참조만 한다.

| 용어 | 정의 |
|---|---|
| **send-back** | 경로 위 agent 사이에서 기록을 받은 쪽이 그 기록을 쓴 agent(작성agent)에게 돌려보내는 것(기록의 `send_back`). 의심되는 원인의 목록(`suspected_causes`)을 싣는다. 원인은 값 문제(공급망계획agent가 요청대로는 안 된다고 해서 supply_coordination이 forecast·채널로 보내는 경우)이거나 `misrouted`(자기 일이 아님)다. 받는 쪽은 항상 그 기록의 작성agent다. 검증agent의 판정은 send-back이 아니다. |
| **불합격 판정** | 검증agent가 기록의 `validation`에 `failed`와 `suspected_causes`를 쓰는 것. 검증agent는 경로 밖이라 되돌리는 것이 아니라 판정을 기록하고, 작성agent가 기록을 읽고 처리한다. |
| **의심되는 원인** | 결과에 문제가 있다고 보는 이유 하나(`suspected_causes`의 항목, 형식은 STATE_SCHEMA.md). 불합격 판정(`validation`)과 send-back(`send_back`)이 모두 이 항목의 목록을 싣는다. |
| **재실행(re-run)** | send-back(`misrouted` 제외)이나 불합격 판정을 받은 작성agent가 자기 내부 단계를 다시 도는 것. agent 내부의 일이다. 같은 입력이면 같은 결과가 나오므로 의심되는 원인을 입력에 넣어 입력을 바꾼 채 돈다. 구체 정의는 지금 forecast만 있고(AGENT_NODE_LIST.md), 다른 agent는 해당 마일스톤에서 정한다. |
| **재개 지점** | 재실행이 시작되는 agent 내부 단계(의심되는 원인으로 정해진다). |
| **요청·응답** | 회의(M5)에서 agent 하나가 다른 agent에게 바라는 것을 쓰거나(요청) 다른 agent의 기록에 답하는 것(응답). 회의 기록 하나가 둘을 함께 가질 수 있다(응답이면서 새 요청). |
| **라운드** | 하나의 입력에 대해 요청·응답을 여러 번 주고받아 결론을 내기까지(M5 회의 하나). 요청·응답 횟수의 상한은 `interaction_protocol`의 `max_exchanges`다. |
| **재조정** | supply_coordination이 자체로 푸는 것. |
| **상위로 확장** | agent 사이에서 같은 이유가 `repeat_escalation_threshold`번 연속 반복되면 `max_exchanges` 소진을 기다리지 않고 위/옆 agent 또는 사람으로 넘기는 것. **재실행(agent 내부)에는 쓰지 않는다.** |
| **제외** | 가정이나 소스를 쓰지 않는 것(`excluded_assumptions`, `excluded_sources`). |
| **소진** | 두 가지이고 섞지 않는다. `max_exchanges` 소진(한 라운드 안의 요청·응답 횟수 상한)과 `options_exhausted`(forecast 재실행으로 가정이 모두 제외됨). |
| **escalation** | 사람(업무 판단은 `human_manager`, 시스템 오류는 `system_operator`)에게 올리는 것(`intervention`/`notice`). |

## 검증agent

검증agent가 무엇을 보는지, 판정만 하고 라우팅하지 않는 원칙과 이유, 판단 방식은 AGENT_NODE_LIST.md 검증agent 절이 정의한다.
이 절은 누가 누구에게 무엇을 보내는지의 흐름만 정리한다.

흐름:
1. 작성agent는 자기 일이 모두 끝난 뒤 기록을 한 번 쓴다(모든 작성agent에 적용한다. 일을 하는 동안 기록에 중간 결과를
   쓰지 않는다). 그 쓰기로 기록이 "검증 대기" 상태가 되면 **원래 의도한 다음 agent가 아니라 검증agent에게만** 신호가 간다.
   어느 검증agent가 언제 검증하는지는 `validation_settings`(STATE_SCHEMA.md 9번 절)가 정한다. 보류로 끝난 일(처리되지 않은
   escalation 기록이 있는 인스턴스)은 기록이 "업무 대기"나 "시스템 대기" 상태가 되므로 검증agent에게 신호가 가지 않는다.
2. 검증agent(워커풀)는 신호를 받으면 기록을 읽어 "검증 대기"일 때만 판정한다. 판정(`validation.status`)과 그 로그 항목(`error`면
   escalation 기록도)을 한 번의 State 갱신으로 쓰면 기록이 "판정됨"(`error`면 "시스템 대기") 상태가 되어 작성agent의
   채널(`error`면 `system_operator`)로 신호가 간다. 다음 agent에게는 전달하지 않는다.
3. 판정별로 다음에 어디로 가는지:
   - **`passed`**: 작성agent가 신호를 받아 기록이 "판정됨"인지 읽고, 다음 agent를 골라 `forward_to`에 써서 **직접** 넘긴다(위 "다음
     agent 선택"). 기록이 "전달됨"이 되어 그 agent에게 신호가 간다. 예: `forecast`가 `supply_coordination`에게. 작성agent의 카드에
     `produces`가 없으면 기록은 바로 "완료"다(예: supply_coordination의 배분 결론, M5 전까지).
   - **`failed`**(불합격 판정): 작성agent가 처리한다(처리 판단은 AGENT_NODE_LIST.md 검증agent 절과 각 agent 절). 같은 이유가 연속
     K회 반복되면 "이 agent 선에서 구조적으로 안 풀림"으로 간주해 `max_exchanges` 소진을 기다리지 않고 **상위로 확장**한다. 이
     판단은 작성agent 쪽의 몫이며 **M5의 회의에서만** 쓴다(forecast의 재실행은 스스로 끝나고, supply_coordination의 배분 결론은 입력이
     바뀐 경우에만 다시 조율하므로 failed의 반복을 셀 필요가 없다). K는 `interaction_protocol`의 `repeat_escalation_threshold`
     (STATE_SCHEMA.md)이고, "몇 번 반복됐는지"는 별도로 저장하지 않고 그 회의의 기록을 최근 것부터 훑어 같은 이유가 연속 몇 개인지
     그때그때 계산한다. **이 카운트는 엣지+이유 단위로만 유효**하다 — 다른 엣지로 넘어가면(예: 상위로 확장돼 다른 agent가 처리) 그
     agent의 기록에서 새로 계산되므로 자동으로 리셋된다(누적 이월 없음).
   - **`error`**(검증 절차 자체가 비정상 종료 — 시스템 장애): 재시도 없이 바로 `escalation_records`에 `system_operator` 담당 escalation
     기록을 만든다(기록이 "시스템 대기"가 되어 system_operator에게 신호가 간다). 기록의 필드 값은 STATE_SCHEMA.md 7번 절을 따른다.
     **그 인스턴스만 멈추고 다른 인스턴스는 진행한다.** 작성agent는 이 판정을 받아도 할 일이 없고, **다음 agent는 이 상태의 레코드를
     받지 않는다**(작성agent는 `passed` 또는 검증 면제 통과인 기록만 보낸다).

### 사람 입력과 검증의 무결성

모든 엣지에 적용하는 공통 규칙이다.
- 처리되지 않은 escalation 기록이 있는 인스턴스의 기록은 "업무 대기"나 "시스템 대기" 상태이므로 검증agent도 다음 agent도 그 기록에
  일하지 않는다(받는 쪽이 상태를 확인한다).
- 그 인스턴스의 작성agent는 첫 실행을 포함해 실행하지 않고, 건너뛴 사실만 자기 로그에 남긴다(기록도 escalation 기록도 새로 만들지
  않는다). 업무 대기는 시한(기본: 다음 정해진 조율 시각)이 지나면 정해진 처리로 끝나고, 시스템 대기는 시한이 없어 처리될 때까지
  이어진다 — 다음 계획 주기가 되어도 그대로다.
- 사람이 기록에 값을 쓰는 경로는 `intervention`의 `resolution`뿐이다. 그 밖의 경로로 사람이 기록을 고치지 않는다. 처리 함수가
  escalation 기록과 값을 받는 기록을 한 번의 State 갱신으로 쓴다(STATE_SCHEMA.md 7번 절).
- 사람이 넣은 값은 넣을 때 입력 제한(0 이상, 계약 단위의 배수, 과거 범위를 크게 벗어나면 경고, 같은 입력의 중복 거부)으로
  확인하고, 검증agent를 다시 거치지 않는다. agent 경계를 넘는 제약(공급 가능량 등)은 그 값이 배분으로 들어간 뒤 배분 결론의
  검증에서 확인된다.
- 검증agent를 다시 거치지 않는다는 것은 판정 칸(`validation`)을 비워 두고 `selection_basis`(`human`)로 표시한다는 뜻이다. 시한이
  지나 정해진 처리로 값을 정한 경우도 같다(`expiry`). 판정 칸에는 검증agent의 판정만 쓴다. 이 기록을 "검증 면제 통과"라고
  부르며 정의는 위 "신호 규칙"에 있다.

## 상호작용 세 가지 유형

- **핸드오프형** (기록을 받은 agent가 자기 일이 아니거나 값에 문제가 있다고 작성agent에게 send-back을 보내는 유형. 값 문제는
  supply_coordination이 forecast에 보내는 역방향·예외 경로): 같은 값을 다듬는 게 아니라 **send-back** — 받은 쪽이 기록의
  `send_back`을 쓰면 기록이 "되돌려짐"이 되어 작성agent에게 신호가 가고, 값 문제이면 forecast는 이전 결과를 이어서 다듬는 게 아니라
  새로 계산해서 덮어씀(현재값만 유지, 이력은 `role_logs`). 공급망계획agent가 요청대로는 안 된다고 했을 때만 열리는 예외 경로 —
  평소엔 아예 열리지 않는다. 이 신호는 "숫자를 조금씩 좁혀가며 밀당"할 대상이 아니라 "선택이 틀렸을 수 있으니 다시 하라"는
  신호이므로, 새 라운드 메커니즘을 만들지 않고 forecast agent 자신의 내부 재실행 메커니즘을 그대로 재사용한다. 이 재실행은 같은
  조율 안에 든다. `suspected_cause`에 따른 재개 지점과 대응은 AGENT_NODE_LIST.md forecast agent "재실행" 참고.
- **최적화** (forecast→supply_coordination 정방향, 평소 경로): forecast가 가정 선택이 정한 최종 요청량(`scenario`)을 넘기면
  supply_coordination이 그 라인의 다른 수요와 함께 배분 규칙으로 배분하는 **단방향 전달** — 라운드가 쌓이지 않는다. 상대의 응답을
  받아 값을 조정하는 절차가 아니라 한 번의 계산으로 끝나므로 "협상"이 아니다. 다만 이건 **협상 응답(값 조정)이 없다는 뜻일 뿐**,
  검증을 아예 안 거친다는 뜻은 아니다 — 검증agent의 판정(위 "검증agent")은 이 엣지에도 똑같이 적용된다.
- **라운드 누적형(협상)** (supply_coordination↔공급망계획agent들, M5): 회의 기록(`meetings`)에 요청·응답을 **누적** — 이전 기록을
  지우지 않고 쌓으며 배분과 계획을 조정한다. 기록은 단순 가부가 아니라 대안(조건이 다른 제안과 그 비용)을 담을 수 있다 — "협상"과
  "일방 통보(예: 검증)"를 가르는 지점. 결론(모두 할 수 있다고 확인된 배분)은 supply_coordination이 `allocations`에 쓴다.

같은 agent 쌍(forecast/supply_coordination) 사이에도 방향에 따라 유형이 갈릴 수 있다 — 평소엔 정방향 최적화만 흐르고, 공급망계획agent의
신호로 supply_coordination이 forecast에 send-back을 보낼 때만 역방향 핸드오프형이 쓰인다(엣지 표 참고).

세 유형 모두 검증agent의 판정을 받는다 — `failed`(불합격 판정)면 작성agent가 처리하고, `error`면 다음 소비자가 그 레코드를
걸러낸다(위 "검증agent" 참고).

## 공급망계획agent 연결 구조 — 회의 (M5)

supply_coordination과 procurement_plan·production_plan·logistics_plan은 **회의 기록(`meetings`)을 함께 읽고 쓰는 방식**으로 연결한다
(STATE_SCHEMA.md "아직 정하지 않은 것"에 회의 기록의 모양). 한 agent가 순서대로 한 명씩 묻는 방식이 아니라, 참여 agent 모두가 서로의
기록을 직접 보고 판단한다.
- **각 agent는 묶이지 않는다**: 회의는 모두가 모여야 시작하는 것이 아니라 각자 자기 때에 와서 읽고 쓰는 공유 기록이다. 아무도 다른
  agent를 기다리며 멈추지 않고, 신호를 받으면 기록을 읽어 자기와 관련 있을 때만 쓴다. 상관없는 agent는 아무것도 쓰지 않아도 된다.
  자기 영역의 사건(예: 라인 고장)을 보고 누구든 먼저 기록을 쓸 수 있다.
- **역할**: 회의 기록은 과정이고, 결론은 supply_coordination이 `allocations`에 쓴다. supply_coordination은 회의를 중계하지 않고, 고객사
  계약과 모든 수요를 아는 유일한 agent로서 배분 판단(누구에게 얼마나)을 가진 참여자다.
- **회의 범위**: 정기 조율은 라인 단위다. 그 뒤 생기는 문제는 **문제가 생긴 자원(라인, 원료, 트럭)을 쓰는 수요들**이 범위다(예: 라인A
  고장 → 라인A를 쓰는 수요, 밀가루 부족 → 밀가루를 쓰는 라인들의 수요). 대체 라인이 있는 item은 두 라인을 잇는다.
- **검증**: plan agent의 기록은 기록마다, supply_coordination은 결론에서만 검증한다(`validation_settings`). 다른 agent는 검증 대상인 기록 중
  통과한 것만 읽는다.
- **충돌 판단**: plan agent가 납기를 넘기는 대안을 내면 아끼는 비용과 영향받는 수요를 함께 쓰고, supply_coordination은 그 대안 때문에
  어기게 되는 비용과 비교한다(AGENT_NODE_LIST.md supply_coordination agent "M5에서 더하는 것").
- **M5에서 먼저 정할 것**: 끝나는 조건과 동시 쓰기.
  - 끝나는 조건(방향): 기록 상태로 계산한다 — 남은 요청이 없고 마지막 배분안에 반대하는 기록이 없으면 결론, 요청·응답 수가
    `max_exchanges`를 넘으면 escalation.
  - 동시 쓰기(방향): 낙관적 동시성 제어 — 기록마다 읽은 마지막 기록 번호(`based_on`)를 적고, 그 사이 새 기록이 생겼으면 쓰기를 거절한다.
    거절된 agent는 새로 더해진 기록만 읽고 다시 판단한다(기록은 이어서 쓰기만 하므로 앞 기록은 바뀌지 않는다).
  - 이 둘을 설계할 수 없으면, supply_coordination이 같은 요청을 필요한 plan agent 모두에게 동시에 보내고 다른 agent의 응답을 다음
    요청에 담는 방식으로 한다.

## 엣지 표

경로 위 agent 사이에 넘길 수 있는지의 원본은 각 agent 카드의 `known_agents`다. 아래 표의 경로 위 agent 행은 연결의 성격(유형, 반복,
종료조건, escalation 대상)을 보여주는 요약이고, `known_agents`에 있는 쌍(되돌림의 경우 작성agent로 돌아가는 쌍)만 적는다. 받는 쪽이
응답하는 방식은 카드의 `response_type`(`optimization` 최적화, `handoff` 핸드오프형, `round_accumulation` 라운드 누적형), 보내는 쪽의
한도(`max_exchanges` 등)는 `interaction_protocol`이 원본이다. 외부 경계·검증agent·사람 행은 카드와 무관하다.

| edge | 유형 | 반복 여부 | 종료조건 | escalation 대상 |
|---|---|---|---|---|
| forecast → supply_coordination (정방향, 평소) | 단방향 전달(최적화) | 아니오 | 그 라인의 조율이 결론을 `allocations`에 씀 | 없음 |
| supply_coordination → forecast (역방향, 예외, M5) | 핸드오프형 — 공급망계획agent가 요청대로는 안 된다고 했고 문제가 수요 값에 있을 때 supply_coordination이 forecast에 send-back을 보냄 | 아니오 | 재실행 완료(재개 지점부터) | 없음(forecast 자신의 가정 선택 판단3계층 — ③ 사람 escalation 포함 — 에 위임) |
| supply_coordination → sales_channel | 단방향(출력) — sales_channel이 "완료"된 `allocations`를 읽음 | 아니오 | 즉시 | 없음 |
| sales_channel → forecast | 단방향(입력) | 아니오 | 즉시(주문 이력·POS·프로모션 일정 유입, 주기 스냅샷 기준) | 없음 |
| sales_channel → supply_coordination | 단방향(입력) — 수주, 계약 조건(최소 구매 약정·공급 보장 물량·계약 단위) | 아니오 | 즉시(주기 스냅샷 기준. 새 수주는 "조율 흐름"의 시점에 조율) | 없음 |
| 사람이 정한 수량 → supply_coordination | 단방향(입력) — 주문이 없는 수량 | 아니오 | 진행 중인 조율이 끝나는 즉시 조율 | 없음 |
| supply_coordination → human_manager | 알림(`commitment_gap`, 진행을 멈추지 않음)과 계약 하한 초과·강한 묶임 초과(`intervention`) | 아니오 | 알림은 즉시, intervention은 사람의 resolution | (최종 단계) |
| 작성 agent → validation agent(들) | 판정(라우팅 권한 없음) | 아니오 | `passed`/`failed`/`error` 판정 | error면 바로 system_operator(`validation_error`) |
| escalation_trigger → human_manager / system_operator | 단방향 | 아니오 | 사람의 resolution 입력 또는 시한 초과 처리 | (최종 단계) |

supply_coordination↔procurement_plan·production_plan·logistics_plan 행(라운드 누적형, 회의)은 plan agent 카드와 supply_coordination의
`produces`·`known_agents`가 추가되는 M5에서 표에 더한다.

## 인스턴스 패턴 — 언제 배열+agent_id, 언제 아닌지

새로운 "복수 인스턴스" 역할이 생길 때마다 매번 다시 고민하지 않도록, 기준을 정리:

- **지속되는 정체성이 있는 안건**(forecast_records처럼 "A회사"에 계속
  매임) → **배열 + agent_id**, 각자 자기 정체성에 매인 값을 유지 — 현재값만 두고
  이력은 `role_logs`에 쌓는다(라인 단위 결론인 `allocations`도 같은 방식으로 `line_id`에 매인다)
- **워커풀처럼 아무나 다음 안건을 집어가는 경우**(validation agent들,
  또는 조달담당자가 여러 명으로 쪼개지는 미래 시나리오) → **별도 배열
  불필요** — 이미 안건 기준으로 존재하는 필드(M5 회의 기록의 `entries[i]` 등)에
  누가 처리했는지는 `handled_by`(선택적 부가 필드)로만 남김. 워커의
  정체성 자체가 기록의 핵심이 아니기 때문.

## 실행층이 실제 agent가 되는 경우 (미래)

지금 물리적 실행의 결과(완료량, 리드타임, 배송지연 등)는 외부 데이터로 돌아오는 값이라 State에 두지 않는다. 나중에 실행 추적
agent(예: 실시간 배송 상태 — 출발/이동중/지연/도착)가 생기면 이건 배분·회의와는 **다른 계층의 사건**(Step1 원칙1)이라
`allocations`나 회의 기록 안이 아니라 **새 최상위 필드(`execution_records[]` 등)**로 분리하고, `allocations`의 항목을
참조(포함이 아니라 링크)하는 구조로 가야 함 — 풀 소속을 기준 정보로 찾고 forecast 기록에 저장하지 않은 것과 같은 패턴. 지금은
실행층이 agent가 아니라 외부 경계라 해당 없음.
