"""가정 검증 조건 — 대상 agent의 계산을 재현하지 않는 독립 제약조건 (순수 함수).

STATE_SCHEMA.md의 가정 검증 조건을 구현한다. 검증agent 연결은 M3이고 여기서는 위반을 `SuspectedCause`
(`type: "assumption"`)로 돌려주는 함수만 둔다.

- `no_evidence`: 가정의 driver가 가리키는 근거 `(kind, item_scope)`가 이번 주기 스냅샷의 `data_sources`에 있는가
- `value_out_of_range`: 가정의 `value`가 과거 월별 요청량의 [최소, 최대]에서 범위 폭의
  `ASSUMPTION_VALUE_RANGE_MARGIN`배를 넘게 벗어나지 않는가
- `not_distinct`: 같은 구성의 가정이 둘 이상이 아닌가. 구성은 driver(요인, 근거)와 통계기법 집합이다.
  **요청량이 같아도 데이터나 기법이 다르면 다른 가정**이라 value는 비교하지 않는다
- `double_counted`: 한 가정 안의 두 driver가 같은 근거를 중복 사용하지 않는가(예: 가격 인하가 포함된
  프로모션을 `price`와 `event`로 이중 계산)
"""

import numpy as np

from .judgment_thresholds import ASSUMPTION_VALUE_RANGE_MARGIN
from .state import Assumption, AssumptionIssue, DataSource, SuspectedCause


def _cause(issue: AssumptionIssue, assumption_id: str) -> SuspectedCause:
    return SuspectedCause(type="assumption", issue=issue, assumption_id=assumption_id)


def _signature(assumption: Assumption) -> tuple:
    drivers = sorted(
        (d.driver, d.evidence.kind, d.evidence.item_scope, tuple(sorted(d.evidence.refs))) for d in assumption.drivers
    )
    return tuple(drivers), tuple(sorted(m.method for m in assumption.method_values))


def validate_assumptions(
    assumptions: list[Assumption], data_sources: list[DataSource], history: list[float]
) -> list[SuspectedCause]:
    """위반한 가정마다 되돌림 사유를 반환한다. 위반이 없으면 빈 목록이다."""
    causes: list[SuspectedCause] = []
    available = {(s.kind, s.item_scope) for s in data_sources}
    low, high = (min(history), max(history)) if history else (None, None)

    for assumption in assumptions:
        if any((d.evidence.kind, d.evidence.item_scope) not in available for d in assumption.drivers):
            causes.append(_cause("no_evidence", assumption.assumption_id))
        evidence_keys = [(d.evidence.kind, d.evidence.item_scope, tuple(sorted(d.evidence.refs))) for d in assumption.drivers]
        if len(set(evidence_keys)) < len(evidence_keys):
            causes.append(_cause("double_counted", assumption.assumption_id))
        if assumption.value is not None and low is not None and high is not None:
            margin = ASSUMPTION_VALUE_RANGE_MARGIN * max(high - low, 1e-9)
            if not (low - margin <= assumption.value <= high + margin) or not np.isfinite(assumption.value):
                causes.append(_cause("value_out_of_range", assumption.assumption_id))

    seen: dict[tuple, str] = {}
    for assumption in assumptions:
        key = _signature(assumption)
        if key in seen:
            causes.append(_cause("not_distinct", assumption.assumption_id))
        else:
            seen[key] = assumption.assumption_id
    return causes
