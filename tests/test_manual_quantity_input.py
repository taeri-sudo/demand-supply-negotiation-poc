"""사람이 정한 수량을 넣는 함수 (AGENT_NODE_LIST.md "외부 경계"): 입력 제한, 변경 알림의 작성자, 이어지는 조율로 들어가는 경로.

[테스트 전용 입력] 합성 샘플(`provider_fixtures.py`)과 fixture 인스턴스를 쓴다.
"""

from datetime import date

import pytest

from coordination_fixtures import SETTINGS, SUPPLY, decided, deliver, entries_by_ref, make_store, pump
from provider_fixtures import AS_OF, write_sample
from sop.line_data_provider import SampleLineDataProvider
from sop.manual_quantity_input import ManualInputRejected, enter_manual_quantity
from sop.supply_coordination_agent import SupplyCoordinator

pytestmark = pytest.mark.anyio

HUMAN = "human_manager"


@pytest.fixture
def provider(tmp_path) -> SampleLineDataProvider:
    return SampleLineDataProvider(date.fromisoformat(AS_OF), write_sample(tmp_path / "sample"))


def signals(store) -> list[dict]:
    queue, messages = store.queue(SUPPLY), []
    while not queue.empty():
        messages.append(queue.get_nowait())
    return messages


def test_an_entered_quantity_joins_the_data_of_its_items_line_and_notifies_that_line_as_the_person(provider):
    store = make_store()

    demand = enter_manual_quantity(store, provider, company_id="A", item_id="I3", quantity=60)

    assert demand.demand_id.startswith("MAN-") and (demand.company_id, demand.item_id, demand.quantity) == ("A", "I3", 60.0)
    assert demand in provider("L2").manual and demand not in provider("L1").manual
    (message,) = signals(store)
    assert message["record_path"] == "allocations[1]"  # 라인 L2의 기록
    assert message["written_by"] == HUMAN != "sales_channel"  # 사람이 넣은 값은 sales_channel로 남지 않는다


def test_a_quantity_without_a_customer_is_accepted_and_the_unit_is_the_item_default(provider):
    store = make_store()

    enter_manual_quantity(store, provider, company_id=None, item_id="I1", quantity=18)
    with pytest.raises(ManualInputRejected) as error:
        enter_manual_quantity(store, provider, company_id="B", item_id="I1", quantity=18)  # 고객사 B의 계약 단위는 12다

    assert error.value.code == "not_unit_multiple"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"item_id": "NOPE", "quantity": 6}, "unknown_item"),
        ({"item_id": "I1", "quantity": -6}, "negative"),
        ({"item_id": "I1", "quantity": "60"}, "not_a_number"),
        ({"item_id": "I1", "quantity": float("nan")}, "not_a_number"),
        ({"item_id": "I1", "quantity": True}, "not_a_number"),
        ({"item_id": "I1", "quantity": 7}, "not_unit_multiple"),
        ({"item_id": "I9", "quantity": 120, "company_id": None}, "duplicate"),  # 파일에 이미 같은 수량이 있다
    ],
)
def test_a_value_that_breaks_an_input_limit_is_rejected_and_nothing_changes(provider, kwargs, code):
    store = make_store()
    before = [provider("L1"), provider("L2")]

    with pytest.raises(ManualInputRejected) as error:
        enter_manual_quantity(store, provider, **{"company_id": "A", **kwargs})

    assert error.value.code == code
    assert [provider("L1"), provider("L2")] == before and signals(store) == []  # 거부하면 데이터도 알림도 바뀌지 않는다


def test_the_same_quantity_entered_twice_is_a_duplicate_and_zero_is_valid(provider):
    store = make_store()
    enter_manual_quantity(store, provider, company_id="A", item_id="I2", quantity=60)

    with pytest.raises(ManualInputRejected) as error:
        enter_manual_quantity(store, provider, company_id="A", item_id="I2", quantity=60)
    enter_manual_quantity(store, provider, company_id="A", item_id="I2", quantity=0)

    assert error.value.code == "duplicate"
    assert [m.quantity for m in provider("L1").manual if m.item_id == "I2"] == [60.0, 0.0]


async def test_an_entered_quantity_is_coordinated_right_after_the_regular_coordination_as_a_human_change(provider):
    store = make_store()
    agent = SupplyCoordinator(store, SETTINGS, provider, provider.line_of)
    for company, item, value in [("A", "I1", 600), ("B", "I1", 300), ("A", "I2", 300), ("A", "I3", 300)]:
        deliver(store, company, item, value)
    await pump(agent)
    before = len([e for e in decided(store) if e.payload["line_id"] == "L1"])

    enter_manual_quantity(store, provider, company_id="A", item_id="I1", quantity=60)  # 사람이 정한 수량은 하루를 기다리지 않는다
    results = await pump(agent)

    assert [r.outcome for r in results] == ["coordinated"]
    after = [e for e in decided(store) if e.payload["line_id"] == "L1"]
    assert len(after) == before + 1 and after[-1].payload["kind"] == "continuing"
    new_ref = next(m.demand_id for m in provider("L1").manual if m.quantity == 60.0 and m.item_id == "I1")
    assert (new_ref, "soft") in entries_by_ref(store, "L1")
