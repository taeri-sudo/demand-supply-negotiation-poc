"""경로 위 agent의 카드(`agent_cards`, STATE_SCHEMA.md 8번 절)와 카드로 하는 판단.

- 다음 agent 후보: 작성agent 카드의 `known_agents` 중 그 카드의 `accepts`가 작성agent의 `produces`와 같은 agent다.
- 받는 쪽은 자기 카드의 `accepts`가 기록을 낸 agent의 `produces`와 맞는지로 자기 일인지 본다.
"""

from .state import AgentCard

FORECAST_ROLE_TAG = "forecast"
SUPPLY_COORDINATION_ROLE_TAG = "supply_coordination"
VALIDATED_DEMAND_FORECAST = "검증을 통과한 수요 예측"


def forecast_card() -> AgentCard:
    return AgentCard(
        role_tag=FORECAST_ROLE_TAG,
        description="가정을 정의하고 계산해 다음 달 요청량을 정한다",
        produces=VALIDATED_DEMAND_FORECAST,
        response_type="handoff",
        known_agents=[SUPPLY_COORDINATION_ROLE_TAG],
    )


def supply_coordination_card() -> AgentCard:
    return AgentCard(
        role_tag=SUPPLY_COORDINATION_ROLE_TAG,
        description="요청량을 우선순위에 따라 배분안으로 만든다",
        accepts=VALIDATED_DEMAND_FORECAST,
        response_type="optimization",
        known_agents=[],
    )


def default_agent_cards() -> list[AgentCard]:
    return [forecast_card(), supply_coordination_card()]


def card_of(cards: list[AgentCard], role_tag: str) -> AgentCard | None:
    return next((card for card in cards if card.role_tag == role_tag), None)


def candidate_agents(cards: list[AgentCard], author_role_tag: str, excluded: set[str] = frozenset()) -> list[str]:  # pyright: ignore[reportArgumentType]
    """`passed` 뒤에 넘길 수 있는 후보: 작성agent 카드의 `known_agents` 중 `accepts`가 작성agent의 `produces`와 같은 agent(`excluded` 제외)."""
    author = card_of(cards, author_role_tag)
    if author is None or author.produces is None:
        return []
    candidates: list[str] = []
    for role_tag in author.known_agents:
        known = card_of(cards, role_tag)
        if role_tag not in excluded and known is not None and known.accepts == author.produces:
            candidates.append(role_tag)
    return candidates


def accepts_record(cards: list[AgentCard], receiver_role_tag: str, author_role_tag: str) -> bool:
    """받는 agent가 작성agent의 기록을 자기 일로 받는지: 자기 카드의 `accepts`가 작성agent의 `produces`와 같은가."""
    receiver, author = card_of(cards, receiver_role_tag), card_of(cards, author_role_tag)
    return receiver is not None and author is not None and receiver.accepts is not None and receiver.accepts == author.produces
