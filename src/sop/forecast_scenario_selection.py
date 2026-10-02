"""forecast agent의 시나리오 선택 판단 (M1 스텁).

AGENT_NODE_LIST.md forecast agent 6단계는 이 선택을 판단3계층으로 나눈다 —
① 규칙: `cost_estimate`가 가장 낮은 시나리오 선택, ② 판단: 상위 두 시나리오의
`cost_estimate`가 거의 같으면 "애매함" 표시, ③ 사람: 시나리오끼리 예측값이 크게
갈리는데 발생 가능성도 비슷하면 escalation.

지금은 ①만 구현한다 — ②/③ 분기 조건과 최소 구매 약정 적용은 M2 4단계에서
붙는다. 반환 스키마는 `StructuredJudgment`로 고정해 조건 분기나 LLM 판단(M7)이
붙어도 호출부가 안 바뀌게 한다(공통 규칙 2).
"""

from .judgment import StructuredJudgment
from .state import Scenario


def select_forecast_scenario(scenarios: list[Scenario]) -> StructuredJudgment:
    costed = [(s.cost_estimate, s) for s in scenarios if s.cost_estimate is not None and s.value is not None]
    if len(costed) != len(scenarios):
        raise ValueError("시나리오 선택은 value/cost_estimate 계산이 끝난 시나리오만 받음")
    _, best = min(costed, key=lambda pair: pair[0])
    return StructuredJudgment(
        judgment={"selected": best.scenario_id, "value": best.value},
        reasoning=(
            f"시나리오 {[s.scenario_id for s in scenarios]} 중 cost_estimate가 가장 낮은 "
            f"'{best.scenario_id}'(cost_estimate={best.cost_estimate})를 규칙 기반으로 채택"
        ),
    )
