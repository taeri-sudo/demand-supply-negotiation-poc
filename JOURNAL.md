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

---

## 2026-09-16 — M1: candidate 선택 테스트 공백과 판단 스키마 통합

**candidate 선택 단계 누락이 테스트를 통과시킨 이유**: Claude Code가
`run_forecast_supply_round`(협상 라운드 루프)를 `initial_proposed`를 함수
인자로 직접 받는 구조로 구현했다. forecast의 candidate 선택 판단(analysis
스텁의 a/b/c 중 하나를 골라 `forecast_agents[i].selected`/`selection_basis`를
채우는 단계)을 구현하지 않은 채로도, 테스트가 `initial_proposed`를 직접
넘겨 라운드 로직만 검증할 수 있어 pytest 전체가 통과했다 — candidate 선택
경로 자체를 한 번도 실행하지 않고도 관련 테스트가 초록불이었다는 뜻이다.
이후 `run_forecast_negotiation`(candidate 선택 → 라운드 루프 연결)을 추가하고,
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
