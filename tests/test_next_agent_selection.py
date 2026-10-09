"""다음 agent 선택과 받는 쪽 반송(GRAPH_FLOW.md "다음 agent 선택", "받는 쪽 반송"), 채널은 agent의 role_tag 하나.

[테스트 전용 입력] 가정 값은 `assumption_fixtures`의 고정 가정을 쓰고, 카드는 `agent_cards`의 기본 카드에서 known_agents와 accepts만 바꾼다.
"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from assumption_fixtures import fixed_assumptions
from sop.access import ForwardTargetRejected, StateStore
from sop.agent_cards import VALIDATED_DEMAND_FORECAST, default_agent_cards
from sop.escalation_records import urgency_of
from sop.forecast_steps import ForecastRerunHandling
from sop.forecast_supply_allocation import (
    allocate_validated_forecast,
    check_forecast_verdict,
    forward_validated_forecast,
    run_forecast_select_and_submit,
    send_back_misrouted,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.record_state import record_state
from sop.state import AgentCard, ForecastRecord, RolePermission, SendBack, SuspectedCause, ValidationResult
from sop.validation_agent import process_pending_validations
from test_forecast_rerun import first_run, make
from validation_fixtures import FORECAST, SUPPLY, forecast_rules, make_store

pytestmark = pytest.mark.anyio

RAMEN = "COMPANY-A:RAMEN"
OTHER = "other_agent"
OTHER_PERMISSIONS = tuple(
    RolePermission(role_tag=OTHER, field_path=field, access=access)  # pyright: ignore[reportArgumentType]
    for field, access in [
        ("forecast_records", "r"), ("escalation_records", "r"), ("agent_cards", "r"),
        ("forecast_records[*].send_back", "w"), (f"role_logs.{OTHER}", "w"), ("role_logs", "r"),
    ]
)


def cards(known: list[str], other_accepts: str | None = None) -> list[AgentCard]:
    """forecast의 known_agents를 바꾼 기본 카드. `other_accepts`를 주면 other_agent 카드를 더한다."""
    result = [
        card.model_copy(update={"known_agents": known}) if card.role_tag == FORECAST else card
        for card in default_agent_cards()
    ]
    if other_accepts is not None:
        result.append(AgentCard(role_tag=OTHER, description="테스트용", accepts=other_accepts, response_type="optimization"))
    return result


async def passed_store(card_list: list[AgentCard] | None = None) -> StateStore:
    """판정이 `passed`가 된 기록 하나가 있는 store. 아직 넘기지 않았다."""
    store = make_store(
        [ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN")],
        extra_permissions=OTHER_PERMISSIONS, cards=card_list,
    )
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    assert store.state.forecast_records[0].validation.status == "passed"  # pyright: ignore[reportOptionalMemberAccess]
    return store


def state_of(store: StateStore) -> str:
    return record_state(store.state.forecast_records[0], store.state.escalation_records)


def pending(store: StateStore) -> dict[str, int]:
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


def drain(store: StateStore) -> None:
    for queue in store._queues.values():  # pyright: ignore[reportPrivateUsage]
        while not queue.empty():
            queue.get_nowait()


def handling() -> ForecastRerunHandling:
    """재실행하지 않는 경로만 쓰므로 인스턴스 식별자만 RAMEN으로 맞춘 입력을 쓴다."""
    return ForecastRerunHandling(replace(make(), company_id="COMPANY-A", item_id="RAMEN"), first_run())


# --- 채널은 agent의 role_tag 하나 ----------------------------------------------------------------------------------


async def test_every_signal_goes_to_the_role_tag_channel_of_the_owner_of_the_state():
    store = make_store([ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN")])
    seen: dict[str, str] = {}

    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    seen[state_of(store)] = next(iter(pending(store)))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())  # 검증agent가 자기 채널의 신호를 꺼내 판정한다
    seen[state_of(store)] = next(iter(pending(store)))
    drain(store)
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    seen[state_of(store)] = next(iter(pending(store)))
    drain(store)
    send_back_misrouted(store, SUPPLY, "forecast_records[0]")
    seen[state_of(store)] = next(iter(pending(store)))

    assert seen == {
        "awaiting_validation": VALIDATOR_ROLE_TAG, "judged": FORECAST, "forwarded": SUPPLY, "sent_back": FORECAST
    }


# --- 카드로 다음 agent를 고른다 ---------------------------------------------------------------------------------------


async def test_the_forecast_picks_supply_coordination_by_the_cards():
    store = await passed_store()
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, handling()) == "forwarded"

    assert store.state.forecast_records[0].forward_to == SUPPLY and state_of(store) == "forwarded"
    assert pending(store) == {SUPPLY: 1}
    (candidate,) = [allocate_validated_forecast(store, SUPPLY, store.queue(SUPPLY).get_nowait())]
    assert candidate is not None and candidate.allocation == {RAMEN: 120.0}


@pytest.mark.parametrize(
    "card_list",
    [
        cards([]),  # known_agents가 비어 있다
        [  # known_agents에 있지만 accepts가 맞지 않는다
            card.model_copy(update={"accepts": "다른 입력"}) if card.role_tag == SUPPLY else card for card in default_agent_cards()
        ],
    ],
    ids=["no_known_agents", "accepts_mismatch"],
)
async def test_no_candidate_makes_a_no_next_agent_escalation(card_list):
    store = await passed_store(card_list)
    drain(store)
    before = store.state.forecast_records[0]

    assert await check_forecast_verdict(store, FORECAST, handling()) == "next_agent_unresolved"

    (escalation,) = store.state.escalation_records
    assert escalation.reason == "no_next_agent" and escalation.agent_id == RAMEN and escalation.mode == "intervention"
    assert urgency_of(escalation.reason) == "warning"
    record = store.state.forecast_records[0]
    assert record.forward_to is None and record.scenario == before.scenario and record.validation == before.validation
    assert state_of(store) == "awaiting_human" and pending(store) == {"human_manager": 1}
    assert allocate_validated_forecast(store, SUPPLY, {"field_path": "forecast_records[0]"}) is None


async def test_several_candidates_make_a_multiple_next_agents_escalation():
    store = await passed_store(cards([SUPPLY, OTHER], other_accepts=VALIDATED_DEMAND_FORECAST))
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, handling()) == "next_agent_unresolved"

    (escalation,) = store.state.escalation_records
    assert escalation.reason == "multiple_next_agents" and urgency_of(escalation.reason) == "warning"
    assert store.state.forecast_records[0].forward_to is None and pending(store) == {"human_manager": 1}
    entry = next(e for e in store.negotiation_log(FORECAST) if e.event == "next_agent_unresolved")
    assert entry.payload["candidates"] == [SUPPLY, OTHER] and escalation.log_seq == entry.seq


async def test_a_forward_to_outside_known_agents_is_rejected():
    store = await passed_store()
    before = store.state
    drain(store)

    with pytest.raises(ForwardTargetRejected):
        store.set_field(FORECAST, "forecast_records[0].forward_to", "stranger")

    assert store.state is before and pending(store) == {}


async def test_a_forward_to_is_rejected_when_the_author_has_no_card():
    store = await passed_store([card for card in default_agent_cards() if card.role_tag != FORECAST])

    with pytest.raises(ForwardTargetRejected):
        store.set_field(FORECAST, "forecast_records[0].forward_to", SUPPLY)


# --- 받는 쪽 반송 -----------------------------------------------------------------------------------------------------


async def test_a_receiver_whose_card_does_not_accept_the_record_sends_it_back_as_misrouted():
    store = await passed_store()
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    drain(store)
    mismatched = [  # 받는 쪽의 카드가 바뀌어 더는 이 기록을 받지 않는다
        card.model_copy(update={"accepts": "다른 입력"}) if card.role_tag == SUPPLY else card for card in store.state.agent_cards
    ]
    store._state = store.state.model_copy(update={"agent_cards": mismatched})  # pyright: ignore[reportPrivateUsage]

    assert allocate_validated_forecast(store, SUPPLY, {"field_path": "forecast_records[0]"}) is None

    record = store.state.forecast_records[0]
    assert record.send_back is not None and record.send_back.is_misrouted and record.send_back.from_role == SUPPLY
    assert state_of(store) == "sent_back" and pending(store) == {FORECAST: 1}
    assert store.get_field(SUPPLY, "allocation_candidates") == []
    entry = next(e for e in store.negotiation_log(FORECAST) if e.event == "misrouted_send_back")
    assert entry.role_tag == SUPPLY and entry.payload == {"from_role": SUPPLY, "validation_ts": record.validation.ts}  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_misrouted_record_is_not_rerun_and_the_sender_is_left_out_of_the_candidates():
    store = await passed_store(cards([SUPPLY, OTHER], other_accepts=VALIDATED_DEMAND_FORECAST))
    store.set_field(FORECAST, "forecast_records[0].forward_to", OTHER)  # 후보가 둘이라 직접 쓴다(둘 다 known_agents 안)
    before = store.state.forecast_records[0]
    send_back_misrouted(store, OTHER, "forecast_records[0]")
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, handling()) == "forwarded"

    record = store.state.forecast_records[0]
    assert record.forward_to == SUPPLY and record.send_back is None and state_of(store) == "forwarded"
    assert record.scenario == before.scenario and record.assumptions == before.assumptions and record.validation == before.validation
    assert not [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]  # 재실행하지 않았다
    assert store.state.escalation_records == [] and pending(store) == {SUPPLY: 1}
    candidate = allocate_validated_forecast(store, SUPPLY, store.queue(SUPPLY).get_nowait())
    assert candidate is not None and candidate.allocation == {RAMEN: 120.0}


async def test_no_candidate_left_after_the_misroute_makes_a_no_next_agent_escalation():
    store = await passed_store()
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    send_back_misrouted(store, SUPPLY, "forecast_records[0]")
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, handling()) == "next_agent_unresolved"

    (escalation,) = store.state.escalation_records
    assert escalation.reason == "no_next_agent" and "supply_coordination" in escalation.rationale
    record = store.state.forecast_records[0]
    assert record.forward_to is None and record.send_back is None and state_of(store) == "awaiting_human"
    assert pending(store) == {"human_manager": 1}
    assert not [e for e in store.negotiation_log(FORECAST) if e.event == "rerun"]


async def test_a_return_for_an_older_judgment_does_not_exclude_the_agent_for_a_new_one():
    store = await passed_store()
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    send_back_misrouted(store, SUPPLY, "forecast_records[0]")
    record = store.state.forecast_records[0]
    # 같은 기록이 다시 검증받아 새 판정이 되면 이전 판정에 대한 반송은 후보를 줄이지 않는다
    renewed = record.model_copy(update={
        "send_back": None, "forward_to": None, "validation": ValidationResult(status="passed", ts="newer-judgment"),
    })
    store.set_field(FORECAST, "forecast_records[0]", renewed)

    assert await check_forecast_verdict(store, FORECAST, handling()) == "forwarded"
    assert store.state.forecast_records[0].forward_to == SUPPLY


async def test_a_receiver_only_returns_a_record_forwarded_to_itself():
    store = await passed_store()

    with pytest.raises(ValueError):
        send_back_misrouted(store, SUPPLY, "forecast_records[0]")  # 아직 전달되지 않았다


# --- misrouted 이유의 형식 -----------------------------------------------------------------------------------------


def test_misrouted_has_no_other_fields_and_only_goes_into_send_back():
    misrouted = SuspectedCause(type="misrouted")
    assert misrouted.issue is None
    with pytest.raises(ValidationError):
        SuspectedCause(type="misrouted", issue="insufficient")
    with pytest.raises(ValidationError):
        ValidationResult(status="failed", suspected_causes=[misrouted])  # 검증agent는 되돌리지 않는다
    with pytest.raises(ValidationError):
        SendBack(from_role=SUPPLY, suspected_causes=[misrouted, SuspectedCause(type="method_selection")], ts="t")
    assert SendBack(from_role=SUPPLY, suspected_causes=[misrouted], ts="t").is_misrouted
    assert not SendBack(from_role=SUPPLY, suspected_causes=[SuspectedCause(type="method_selection")], ts="t").is_misrouted
