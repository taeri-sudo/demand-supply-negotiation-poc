"""forecast agent의 "가정 선택" 단계 (판단3계층, 규칙 기반).

가정들의 `value` 중에서 하나를 택하거나, 값이 너무 갈리면 중간값이나 평균을 계산해서 최종 요청량(시나리오)을
정하고, 가정이 하나뿐이면 그대로 전달한다. 숫자 공식 하나로 계산하지 않는다. M2에서는 규칙이 이 자리를
채우고 M7에서 LLM이 같은 반환 스키마로 이어받는다.

규칙(①), 상대 범위는 (최대 - 최소) / 중앙값:
1. 가정이 하나면 그대로 전달한다(`pass_through`).
2. 상대 범위가 `ASSUMPTION_CLOSE_REL_RANGE` 이하면 값이 매우 비슷하니 평균을 낸다(`mean`).
3. 가장 그럴듯한 가정의 점수가 두 번째의 `ASSUMPTION_CLEAR_LEADER_RATIO`배 이상이면 그 가정의 값을 택한다
   (`chosen`). 상대 범위가 `ASSUMPTION_SPLIT_REL_RANGE` 이상이면 다른 가정과 크게 갈린다는 뜻이라 "애매함".
4. 뚜렷한 가정이 없으면(점수를 못 구한 경우 포함) 평균을 내고 "애매함"으로 표시한다. 상대 범위가
   `ASSUMPTION_SPLIT_REL_RANGE` 이상이면 평균 대신 중간값을 내고 사람 escalation이 필요하다고 표시한다
   (②판단/③사람 계층).

"그럴듯함"의 점수는 가정 발생 가능성(`occurrence_likelihood`)이 모든 가정에 있으면 그 값이고, 없으면
가정의 상대 오차(`forecast_uncertainty` / `value`)의 역제곱이다(과거 정확도가 높은 가정이 더 그럴듯하다).
둘 다 구할 수 없으면 점수가 없다. 가정이 `MAX_ASSUMPTIONS_FOR_SELECTION`개를 넘으면 상대 오차가 작은 순으로
남긴다.
"""

from .judgment import StructuredJudgment
from .judgment_thresholds import (
    ASSUMPTION_CLEAR_LEADER_RATIO,
    ASSUMPTION_CLOSE_REL_RANGE,
    ASSUMPTION_SPLIT_REL_RANGE,
    MAX_ASSUMPTIONS_FOR_SELECTION,
)
from .logging_utils import log
from .state import Assumption, EscalationRecord

ROLE_TAG = "forecast"
ASSUMPTION_SPLIT_EDGE = "forecast->human_manager"
_TOLERANCE_DIGITS = 9  # 기준값 경계에서 부동소수점 오차로 판정이 갈리지 않게 반올림


def _relative_error(assumption: Assumption) -> float | None:
    if assumption.forecast_uncertainty is None or not assumption.value:
        return None
    return assumption.forecast_uncertainty / assumption.value


def _plausibility_scores(assumptions: list[Assumption]) -> list[float] | None:
    if all(a.occurrence_likelihood is not None for a in assumptions):
        return [a.occurrence_likelihood or 0.0 for a in assumptions]
    errors = [_relative_error(a) for a in assumptions]
    if all(e is not None and e > 0 for e in errors):
        return [1.0 / (e or 1.0) ** 2 for e in errors]
    return None


def _result(
    value: float, assumptions: list[Assumption], derivation: str, reasoning: str, ambiguous: bool = False,
    ambiguity_reason: str | None = None, escalate: bool = False,
) -> StructuredJudgment:
    return StructuredJudgment(
        judgment={
            "scenario": {"value": value, "assumption_ids": [a.assumption_id for a in assumptions],
                         "derivation": derivation},
            "escalate": escalate,
        },
        reasoning=reasoning,
        ambiguous=ambiguous,
        ambiguity_reason=ambiguity_reason,
    )


def select_forecast_assumption(assumptions: list[Assumption]) -> StructuredJudgment:
    if not assumptions or any(a.value is None for a in assumptions):
        raise ValueError("가정 선택은 value 계산이 끝난 가정만 받음")
    candidates = list(assumptions)
    if len(candidates) > MAX_ASSUMPTIONS_FOR_SELECTION:
        candidates = sorted(
            candidates, key=lambda a: (_relative_error(a) is None, _relative_error(a) or 0.0)
        )[:MAX_ASSUMPTIONS_FOR_SELECTION]
    if len(candidates) == 1:
        only = candidates[0]
        return _result(only.value or 0.0, candidates, "pass_through",
                       f"가정이 '{only.assumption_id}' 하나라 그 값을 그대로 전달")

    values = sorted(a.value or 0.0 for a in candidates)
    middle = len(values) // 2
    median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
    rel_range = round((values[-1] - values[0]) / median, _TOLERANCE_DIGITS) if median > 0 else float("inf")
    mean = sum(values) / len(values)
    ids = [a.assumption_id for a in candidates]

    if rel_range <= ASSUMPTION_CLOSE_REL_RANGE:
        result = _result(mean, candidates, "mean",
                         f"가정 {ids}의 값이 상대 범위 {rel_range:.1%}(기준 {ASSUMPTION_CLOSE_REL_RANGE:.0%} 이하)로 매우 비슷해 평균")
    else:
        scores = _plausibility_scores(candidates)
        leader = None
        if scores is not None:
            order = sorted(range(len(candidates)), key=lambda i: -scores[i])
            second = scores[order[1]]
            if second <= 0 or round(scores[order[0]] / second, _TOLERANCE_DIGITS) >= ASSUMPTION_CLEAR_LEADER_RATIO:
                leader = candidates[order[0]]
        split = rel_range >= ASSUMPTION_SPLIT_REL_RANGE
        if leader is not None:
            result = _result(
                leader.value or 0.0, [leader], "chosen",
                f"가정 '{leader.assumption_id}'의 점수가 두 번째의 {ASSUMPTION_CLEAR_LEADER_RATIO}배 이상으로 가장 그럴듯해 그 값을 택함",
                ambiguous=split,
                ambiguity_reason=(f"가정 {ids}의 상대 범위 {rel_range:.1%}가 기준({ASSUMPTION_SPLIT_REL_RANGE:.0%}) 이상으로 크게 갈림" if split else None),
            )
        elif split:
            result = _result(
                median, candidates, "median",
                f"가정 {ids}의 상대 범위 {rel_range:.1%}가 기준({ASSUMPTION_SPLIT_REL_RANGE:.0%}) 이상으로 갈리는데 뚜렷하게 그럴듯한 가정이 없어 중간값을 내고 사람 판단이 필요",
                ambiguous=True,
                ambiguity_reason="값이 크게 갈리고 가장 그럴듯한 가정을 가릴 수 없음",
                escalate=True,
            )
        else:
            result = _result(
                mean, candidates, "mean",
                f"가정 {ids} 중 뚜렷하게 그럴듯한 가정이 없어 평균",
                ambiguous=True,
                ambiguity_reason="가장 그럴듯한 가정을 가릴 수 없음",
            )
    log(ROLE_TAG, "select_forecast_assumption", derivation=result.judgment["scenario"]["derivation"],
        value=result.judgment["scenario"]["value"], ambiguous=result.ambiguous, escalate=result.judgment["escalate"])
    return result


def escalation_records_for(selection: StructuredJudgment) -> list[EscalationRecord]:
    """사람 escalation이 필요하면 진행을 멈추고 결정을 기다리는 `intervention` 기록을 반환한다.

    State에 쓰는 일은 호출부가 한다.
    """
    if not selection.judgment["escalate"]:
        return []
    return [
        EscalationRecord(
            trigger_edge=ASSUMPTION_SPLIT_EDGE,
            reason=selection.reasoning,
            mode="intervention",
            status="pending",
        )
    ]
