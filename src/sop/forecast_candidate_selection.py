"""forecast agent의 candidate 선택 판단 (M1 스텁).

AGENT_NODE_LIST.md에 따르면 analysis agent가 낸 a/b/c 후보 중 하나를
고르는 건 forecast agent의 판단이고, STATE_SCHEMA.md는 이걸 판단3계층으로
나눈다 — 신뢰구간이 좁으면 규칙(①)으로 자동 채택, 비용-리스크 트레이드오프가
얽히면 agent판단(②), 통계와 비즈니스 판단이 충돌하면 사람(③) escalation.

M1은 이 세 계층 중 "신뢰구간이 좁은가"를 판정하는 조건 분기 자체는 구현하지
않고, confidence가 가장 높은 candidate를 무조건 규칙(①)으로 채택하는
것으로 근사한다 — ②/③ 분기로 갈 조건이 아직 없다(DESIGN.md "아직 결정 안
된 것" 참고). 반환 스키마는 `StructuredJudgment`로 고정해 이후 마일스톤에서
조건 분기나 LLM 판단(M7)이 붙어도 호출부가 안 바뀌게 한다(공통 규칙 2).
"""

from .judgment import StructuredJudgment
from .state import ForecastCandidate


def select_forecast_candidate(candidates: list[ForecastCandidate]) -> StructuredJudgment:
    best = max(candidates, key=lambda c: c.confidence)
    return StructuredJudgment(
        judgment={"selected": best.scenario, "value": best.value},
        reasoning=(
            f"candidate {[c.scenario for c in candidates]} 중 신뢰도가 가장 높은 "
            f"'{best.scenario}'(confidence={best.confidence})를 규칙 기반으로 채택"
        ),
    )
