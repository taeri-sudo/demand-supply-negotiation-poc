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
