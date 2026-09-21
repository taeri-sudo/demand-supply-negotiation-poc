# JOURNAL.md

기록 기준은 CLAUDE.md 참고. 날짜순으로 아래 형식을 이어 쓴다.

---

## 2026-09-15 — M0: field_path 조건부 선택을 wrapper가 해석할지 여부

`get_field`/`set_field`의 field_path에서, STATE_SCHEMA.md 예시 표기
(`allocation_candidates[selected].exchanges`, "자기 role_tag에 해당하는
항목")처럼 조건부로 항목을 고르는 표기를 두 가지 방식으로 비교했다.

- **wrapper가 조건부 세그먼트(`[selected]`, `[role_tag=x]` 등)를 직접
  해석하는 안**: 문서 표기와 코드가 1:1로 대응돼 읽기는 편하지만,
  접근통제 계층이어야 할 wrapper가 "무엇이 selected인지" 같은 도메인
  지식을 갖게 된다. 조건 종류가 늘 때마다(예: "가장 최근 라운드", "특정
  상태인 것") wrapper의 경로 파서도 같이 늘어나야 해서, 범용 인프라와
  도메인 로직의 경계가 흐려진다는 점에서 기각.
- **wrapper는 숫자 인덱스만 받고, 조건 판단은 호출자(agent)가 먼저 해서
  인덱스로 변환해 넘기는 안(채택)**: wrapper는 `capacity_pools[0].remaining_capacity`
  처럼 명확한 경로만 처리하면 되고, "이게 selected인지" 같은 판단은
  agent 판단 로직(M1 이후)의 책임으로 남는다. 호출부마다 "선택된 항목
  찾기" 같은 인덱스 탐색 코드가 반복될 수 있다는 단점은 있으나, 이건
  나중에 agent 쪽에 작은 헬퍼로 뽑아내면 되는 문제이지 wrapper 설계의
  문제는 아니라고 판단했다.

판단 배경은 위 두 안 비교 그대로, 재검토 여부는 DESIGN.md 참고.

---

## 2026-09-15 — M0: anyio는 비교해서 고른 게 아니라 기존 의존성이었음

anyio는 pytest-asyncio와 비교해서 선택한 게 아니라, google-genai가 끌고 온
하위 의존성으로 requirements.txt에 이미 있던 것을 Claude Code가 M0에서
그대로 사용했다 — AnyIO가 자체 pytest 플러그인을 내장하고 있어
pytest-asyncio 없이도 `conftest.py`의 `anyio_backend` 픽스처만으로 비동기
테스트가 정상 동작했다.

---

## 2026-09-16 — M1: capacity_pools 읽기에 Lock을 걸지 여부

`forecast_supply_round.py`의 `run_forecast_supply_round`가 매 라운드마다
`capacity_pools`를 읽어 `supply_coordination_respond`에 넘기는데, 이 읽기
구간에 `capacity.py`의 Lock 패턴(`adjust_capacity_pool`처럼 read 전에
`store.lock(...)`으로 감싸는 것)을 적용할지 검토했다.

- **Lock을 건다(기각)**: `capacity.py`의 기존 Lock은 "읽고 → 계산하고 →
  쓰는" read-modify-write 구간을 보호하기 위한 것(같은 자원을 여러 태스크가
  동시에 **갱신**할 때만 문제). M1의 supply_coordination 응답은
  capacity_pools를 갱신하지 않고 읽기만 하므로 이 패턴이 필요한 상황
  자체가 아니라고 보고 기각.
- **Lock을 안 건다(채택)**: 지금은 forecast agent가 1개뿐이라(회사 1개),
  동시에 같은 pool을 읽는 다른 태스크가 없다 — race 자체가 발생할 수 없는
  상황에 방어코드를 넣는 것은 과설계. 대신 이 전제(회사 1개)가 깨지는
  마일스톤에서 재검토가 필요하다는 점을 DESIGN.md "아직 결정 안 된 것"에
  남겨, 그때 다시 확인하게 했다.

재검토 트리거(회사가 여러 개로 늘어남)는 DESIGN.md 참고.

이후 해당 로직(라운드 협상) 자체가 폐기되며 이 판단도 함께 무효화됨 —
2026-09-19 이후 기록 참고.

---

## 2026-09-16 — M1: candidate 선택 테스트 공백과 판단 스키마 통합

**candidate 선택 단계 누락이 테스트를 통과시킨 이유**: Claude Code가
`run_forecast_supply_round`(협상 라운드 루프)를 `initial_proposed`를 함수
인자로 직접 받는 구조로 구현했다. forecast의 candidate 선택 판단(analysis
스텁의 a/b/c 중 하나를 골라 `forecast_agents[i].selected`/`selection_basis`를
채우는 단계)을 구현하지 않은 채로도, 테스트가 `initial_proposed`를 직접
넘겨 라운드 로직만 검증할 수 있어 pytest 전체가 통과했다 — candidate 선택
경로 자체를 한 번도 실행하지 않고도 관련 테스트가 초록불이었다는 뜻이다.
이후 `run_forecast_select_and_round`(candidate 선택 → 라운드 루프 연결)을 추가하고,
그 경로를 직접 실행하는 통합 테스트를 별도로 만들었다.

**ForecastProposalJudgment를 만들었다가 StructuredJudgment로 통합**:
Claude Code가 처음에 `forecast_next_proposal`의 반환 타입으로 로컬 전용
클래스 `ForecastProposalJudgment(judgment, reasoning)`를 정의했다. 이후
MILESTONES.md의 "공통 규칙 2"(규칙 기반 판단 스텁은 LLM 구조화 출력과 동일한
pydantic 스키마로 반환해, M7에서 내부 구현만 LLM 호출로 교체하고 호출부는
바뀌지 않게 한다)를 확인하고 이를 기각, 모든 판단 스텁이 공유하는
`StructuredJudgment`(`judgment.py`)로 교체했다. 기각 이유: 스텁마다 필드
구조가 같아도 클래스가 다르면 스키마가 여러 곳에 흩어져, "공통 규칙 2"가
보장하려는 인터페이스 안정성(M7에서 호출부 변경 없이 구현체만 교체 가능함)
자체가 깨지기 때문.

---

## 2026-09-16 — M1: candidate 선택에서 판단3계층 조건 분기를 뒤로 미룬 이유

Claude Code가 `select_forecast_candidate`를 구현하며, STATE_SCHEMA.md가
정의한 판단3계층(신뢰구간이 좁으면 규칙①, 비용-리스크 트레이드오프가
얽히면 agent판단②, 충돌하면 사람③) 중 "신뢰구간이 좁은가"를 판정하는
조건 분기를 구현할지, 조건 없이 규칙①만 항상 적용할지 검토했다.

- **조건 분기까지 구현(기각)**: "신뢰구간이 좁다"를 무엇으로 판정할지
  (candidate 간 confidence 격차의 임계치, 표준편차 등)가 STATE_SCHEMA.md/
  MILESTONES.md 어디에도 구체적으로 정의돼 있지 않아, 이 임계치 자체를
  Claude Code가 자체 판단으로 정해야 했다. 이후 analysis 실물(M3)이 실제
  confidence 분포를 내기 전까지는 이 임계치가 실제 데이터로 검증될 수
  없어, 지금 정해봤자 근거 없는 값이 될 것으로 판단해 기각.
- **조건 없이 규칙①만 적용(채택)**: M1의 candidate는 analysis_stub.py의
  하드코딩된 고정값이라 confidence 분포가 애초에 다양하지 않고, 협상 라운드
  로직(M1의 핵심 검증 대상)에는 어떤 candidate가 선택되는지만 영향을
  주지 조건 분기 유무는 영향을 주지 않는다 — 지금 결정해도 검증할 방법이
  없는 조건을 미리 하드코딩하는 대신, 뒤로 미루는 쪽을 택했다.

재검토 트리거(analysis 실물 연동, M3)는 DESIGN.md 참고.

이후 해당 로직(라운드 협상) 자체가 폐기되며 이 판단도 함께 무효화됨 —
2026-09-19 이후 기록 참고.

---

## 2026-09-19 — 인스턴스 단위 재검토: 회사 단위에서 (회사, item) 단위로

발견 경위: 신제품/프로모션 트랙을 설계하다가, forecast_agents의
candidates가 스칼라 하나만 담는 구조라 한 회사가 여러 item(예: 라면과
과자)을 동시에 주문하는 경우를 표현할 수 없다는 게 드러남.

- **회사 안에 item을 중첩시키는 안(기각)**: Step1의 Order-Item 실패
  (한 item의 문제가 같은 Order의 무관한 item까지 막아버렸던 버그)가
  재발할 구조적 위험이 있어 기각.
- **(회사, item)을 평평한 별도 인스턴스로 분리(채택 후보)**: agent_id를
  "A회사:라면"처럼 조합 키로 두면, 한 item의 문제가 다른 item에 영향을
  안 주므로 Step1 문제가 구조적으로 발생하지 않음.
- **capacity_pool의 공유/전용 라인 전환**: 농심 신라면건면 실제 사례로
  확인 — 초기엔 여러 건면 제품이 한 라인을 changeover로 공유하다가,
  수요가 확인되자 전용라인으로 전환함. 이 "공유↔전용 전환" 판단은
  이미 production_plan agent의 판단 범위(스케줄링 결정)에 속하므로
  새 메커니즘 없이 흡수 가능.
- **신규 고객사 온보딩**: 유사 회사 패턴을 신뢰할 근거가 약함(계약조건이
  회사마다 달라 일반화 어려움 — PLAN_LOG의 "리드관리는 agent화 어려움"
  결론과 같은 이유). item별로 human_input 초기값을 받는 온보딩 절차가
  필요할 것으로 봄.

다음 세션에서 STATE_SCHEMA.md 스키마 변경부터 재설계 예정.

---

## 2026-09-20 — 검증agent 관찰형 정정, push/pull 원칙, agent 통합, 엣지 재분류

### 검증agent: "게이트"(라우팅 권한 있음)에서 "관찰형"(판정만)으로 정정
검증agent가 판정 후 다음 목적지로 직접 push하는 설계("critical path를
막는 게이트")를 재검토한 결과 기각 — 이미 각 agent 자신의 기존 역할에
라우팅 권한이 있다(forecast의 핸드오프 되돌림, supply_coordination의
"공급망계획agent 문제 신호 처리"). 검증agent가 라우팅까지 대신하면 이
권한이 중복된다. 판정(값이 문제 없는지)과 라우팅(문제가 생겼을 때 어디로
되돌릴지)을 분리해, 검증agent는 `validation.status`만 State에 쓰고
라우팅은 원래 그 권한을 가진 agent에게 맡기는 쪽으로 정정.

### passed/flagged의 push 여부는 받는 agent의 성격(워커풀 vs 조율)에 따라 갈림
처음엔 "passed는 항상 push 없음, 다음 agent가 알아서 pull"로 정리했으나,
이 pull이 실제로 어떤 트리거로 이뤄지는지 재확인하는 과정에서 문제가
드러남 — "다음 agent가 pull한다"는 설명이 GRAPH_FLOW.md 자신의 Push/Pull
정의(pull은 `queue.get()`, 즉 누군가 먼저 push해야 가져갈 게 있음)와
모순됐다. 검증agent가 push를 안 하면 애초에 다음 agent를 깨울 주체가
없다는 지점을 확인 후 기준을 재정립:
- **워커풀 성격**(트리거되면 반응, 여러 회사 요청을 안건 단위로 처리 —
  검증agent 자신, procurement_plan/production_plan/logistics_plan):
  `passed`는 push 없음 — 안건 단위로 이미 확인하러 가는 게 본래 하는
  일이라 확인 자체가 부담이 아님.
- **조율 성격**(계속 능동적으로 여러 요청을 처리·판단 —
  supply_coordination): `passed`도 push — 우선순위 계산·"공급망계획agent
  문제 신호 처리" 같은 다른 조율 업무로 계속 바빠서, 자기 검증 결과까지
  매번 확인하러 다닐 여유가 없기 때문.
- `flagged`는 agent 성격과 무관하게 항상 작성agent에게 push(기존 유지) —
  pull만으로는 작성agent가 "자기 값에 문제가 생겼는지"를 미리 알 방법이
  없어 깨어나지 못하기 때문.

### analysis agent와 forecast agent를 하나로 통합
두 agent로 나눴던 유일한 이유는 "되돌림 지점이 다르면 agent도 분리"였는데,
되돌림 지점을 재검토한 결과 "데이터 소스 문제"/"모델 선택 문제" 두 값으로
충분하고 "후보 선택만 다시"라는 세 번째 값은 두지 않기로 함 — 모델
선택부터 다시 돌리면 새 후보가 나와 후보 선택도 자연히 다시 이뤄지므로
별도 재개 지점 없이 커버된다. 분리 이유 자체가 사라져 `forecast_agents[]`
하나로 통합(8개 → 7개 최상위 필드).

### supply_coordination→forecast를 라운드 협상에서 핸드오프형으로 재분류
원래 forecast↔supply_coordination 전체를 라운드 누적형 협상(capacity
비교, 격차 좁히기)으로 다뤘으나, infeasible은 "숫자를 조금씩 좁혀가며
밀당"할 대상이 아니라 "선택이 틀렸을 수 있으니 다시 하라"는 신호라는
점에서 재분류. `forecast → supply_coordination`(정방향, 평소)은 단방향
최적화, `supply_coordination → forecast`(역방향, 예외 — 공급망계획agent가
infeasible을 보냈을 때만)는 analysis+forecast 통합 시 만든 핸드오프
메커니즘을 그대로 재사용하는 것으로 정리.

이 재분류로 M1이 구현했던 라운드 협상 코드(격차 50% 좁히기, 변화율
수렴조건, capacity 동시읽기 Lock 여부, 수렴값 저장 위치 등)의 전제가
사라짐 — 관련 JOURNAL 항목(2026-09-16 두 건)에 무효화 표시를 남기고,
DESIGN.md M1 항목도 재작성했다.

---

## 2026-09-21 — M1 코드를 정방향/역방향 분리 설계에 맞춰 리팩터링

2026-09-20에 문서만 정정하고 코드는 그대로 남겨뒀던 라운드 협상 로직
(`forecast_supply_round.py`)을 실제로 걷어내고, `forecast_supply_allocation.py`
(candidate 선택 → `allocation_candidate` 1개 생성, 단방향)로 교체했다.

**capacity_pools/interaction_protocol 미참조를 assert가 아니라 권한
누락으로 증명**: 새 코드가 이 두 필드를 더 이상 안 읽는다는 걸 테스트로
확인할 때, "capacity_pools를 안 읽었다"는 식의 소극적 assert 대신, 테스트
fixture의 `role_permissions`에서 이 두 필드에 대한 권한 자체를 아예
안 줬다 — 코드가 실수로라도 참조하면 `PermissionDenied`로 테스트가
즉시 실패하므로, "참조 안 함"을 더 강하게 보장한다.

**analysis+forecast 스키마 통합은 이번에 같이 안 건드림**: 리팩터링
중 `state.py`가 아직 `AnalysisAgentRecord`/`analysis_agents`를 그대로
갖고 있고 `ForecastAgentRecord`에 라운드 전제 필드(`round_history` 등)가
남아있는 걸 발견했으나, 이 스키마 통합은 M1(candidate 선택→배분 생성)
범위보다 크다 — 되돌림(핸드오프)의 실제 재실행 대상인 데이터 수집·모델
선택 로직 자체가 아직 스텁도 없어서(MILESTONES.md M3 공백), 스키마만
먼저 바꾸면 그 필드를 실제로 채울 코드가 없는 상태로 방치된다. 다음에
데이터 수집/모델 선택 실물(또는 스텁)을 다룰 때 스키마 통합도 같이
하는 쪽을 택했다 — DESIGN.md "아직 결정 안 된 것"에 목록으로 남김.

---

## 2026-09-21 — state.py 스키마 정리(analysis+forecast 통합, 라운드 필드 삭제)

바로 위 리팩터링에서 "손 안 대고 목록으로만 남긴다"고 했던 스키마 불일치를
같은 날 두 번째 패스로 정리했다.

**`data_source_basis`/`model_selection`을 선택 필드로 흡수**: 원래
`AnalysisAgentRecord`에서는 이 두 필드가 필수였다(값을 안 채우면
인스턴스를 만들 수 없음). `ForecastAgentRecord`로 옮기며 `| None = None`
(선택 필드)으로 바꿨다 — 이 필드를 채우는 실제 데이터 소스 판단·모델
선택 로직이 아직 없어서(M3 공백), 필수로 두면 그 로직이 생기기 전까지
`ForecastAgentRecord`를 만들 때마다 의미 없는 placeholder 값을 넣어야
하는 문제가 생긴다. `selected`/`selection_basis`도 이미 같은 이유로
선택 필드였던 것과 일관된 선택.

**`InteractionProtocol.max_rounds`/`repeat_escalation_threshold`는
삭제가 아니라 선택 필드로 전환**: forecast<->supply_coordination에는
더 이상 안 쓰이지만, supply_coordination↔procurement_plan 등(M5)에는
여전히 라운드 상한·반복 임계치가 필요해 필드 자체를 없앨 수는 없었다
— `int | None = None`으로 바꿔 엣지별로 선택적으로 채우게 했다.

`interaction_protocol`을 소비하는 코드는 여전히 없다 — 이전 라운드
로직이 이 필드를 읽던 유일한 코드였는데 그게 삭제됐고, 검증agent(M2)가
아직 구현 안 돼 새 소비자도 없다. 의도된 공백이라 손대지 않음.

기존 pytest 12건은 이 스키마 변경으로 깨진 게 하나도 없었다 — 라운드
전제 필드(`round_history` 등)를 참조하던 테스트가 이미 지난 리팩터링
패스에서 전부 교체됐기 때문.
