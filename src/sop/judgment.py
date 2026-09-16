"""판단③계층 중 ②agent판단(규칙 기반 스텁이든 LLM 구조화 출력이든)의 공통
반환 스키마.

M1 마일스톤 계획의 "공통 규칙 2" — 지금은 규칙 기반 스텁으로 구현하더라도
LLM 구조화 출력과 동일한 이 스키마로 반환해, M7에서 스텁 내부 구현만
LLM 호출로 교체하고 호출부(반환 타입)는 바뀌지 않게 한다.
"""

from pydantic import BaseModel


class StructuredJudgment(BaseModel):
    """판단값 + 근거. `judgment`는 판단 종류마다 다른 자유 dict."""

    judgment: dict
    reasoning: str
