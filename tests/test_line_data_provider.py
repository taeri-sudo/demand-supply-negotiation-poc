"""외부 데이터 제공자 (`SampleLineDataProvider`): 샘플 파일을 라인 단위의 `ExternalLineData`로 읽는다 (AGENT_NODE_LIST.md "외부 경계").

[테스트 전용 입력] 작은 합성 샘플(`provider_fixtures.py`)로 규칙을 확인하고, `data/generated`의 실제 샘플은 모든 라인을 한 번 조율해 보는 것만 확인한다.
"""

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from coordination_fixtures import SETTINGS, SUPPLY, VALIDATOR, deliver, line_state, make_store, pump
from provider_fixtures import AS_OF, write_sample
from sop.allocation_validation import allocation_validation_rules, snapshot_stock_provider
from sop.line_data_provider import SampleLineDataProvider
from sop.supply_allocation_rules import unit_key
from sop.supply_coordination_agent import SupplyCoordinator, notify_cycle_started
from sop.validation_agent import process_pending_validations

pytestmark = pytest.mark.anyio

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "data" / "generated"


@pytest.fixture
def sample(tmp_path) -> Path:
    return write_sample(tmp_path / "sample")


@pytest.fixture
def provider(sample) -> SampleLineDataProvider:
    return SampleLineDataProvider(date.fromisoformat(AS_OF), sample)


def test_lines_and_items_come_from_the_primary_assignments_only(provider):
    assert provider.lines() == ["L1", "L2", "L3"]
    assert [provider.line_of(item) for item in ("I1", "I2", "I9", "I3")] == ["L1", "L1", "L1", "L2"]  # I2의 대체 라인 L2는 쓰지 않는다
    with pytest.raises(KeyError):
        provider.line_of("NOPE")
    with pytest.raises(KeyError):
        provider("L-NOPE")


def test_orders_are_the_open_ones_entered_by_the_snapshot_date(provider):
    data = provider("L1")

    assert {o.demand_id: (o.quantity, o.delivered_quantity, o.due_date) for o in data.orders} == {
        "O2": (300.0, 120.0, date(2017, 8, 5)),  # 미납분: 납기가 지났고 일부만 납품했다
        "O3": (60.0, 0.0, date(2017, 8, 25)),  # 앞으로 납기가 올 수주
    }  # O1은 납품을 마쳤고 O5는 기준일 뒤에 들어왔다
    assert [o.demand_id for o in provider("L2").orders] == ["O4"]


def test_manual_quantities_are_those_entered_by_the_snapshot_date_and_may_have_no_customer(provider):
    (entered,) = provider("L1").manual

    assert (entered.demand_id, entered.company_id, entered.item_id, entered.quantity) == ("MAN-0001", None, "I9", 120.0)
    assert provider("L2").manual == []  # MAN-0002는 기준일 뒤에 입력됐다


def test_the_taken_quantity_of_a_commitment_is_what_was_delivered_inside_its_period_by_the_snapshot_date(provider):
    (commitment,) = provider("L1").commitments

    assert (commitment.company_id, commitment.item_id, commitment.binding, commitment.total_quantity) == ("A", "I1", True, 1200.0)
    assert commitment.taken_quantity == 720.0  # O1 600 + O2 120
    assert [(g.company_id, g.item_id, g.quantity) for g in provider("L1").guarantees] == [("A", "I2", 30.0)]


def test_contract_units_cover_every_customer_and_item_of_the_line_with_company_overrides(provider):
    units = provider("L1").units

    assert units[unit_key(None, "I1")] == 6.0
    assert units[unit_key("A", "I1")] == 6.0
    assert units[unit_key("B", "I1")] == 12.0  # 고객사별 덮어쓰기가 상품별 기본값보다 우선한다
    assert unit_key("A", "I3") not in units  # 다른 라인의 item은 넣지 않는다
    assert units[unit_key(None, "I9")] == 6.0  # 인스턴스가 없는 신제품도 단위가 있다


def test_the_margin_per_unit_is_supply_price_minus_manufacturing_cost_of_the_items_industry(provider):
    assert provider("L1").margins == {"I1": pytest.approx(0.24), "I2": pytest.approx(0.24), "I9": pytest.approx(0.24)}  # 신제품은 같은 라인 item의 업종을 쓴다
    assert provider("L2").margins == {"I3": pytest.approx(0.40)}


def test_an_item_whose_industry_cannot_be_known_is_an_error_not_a_default(provider):
    with pytest.raises(KeyError):
        provider("L3")  # 라인 L3에는 업종을 알 수 있는 item이 없다


def test_stock_and_work_in_progress_are_the_lines(provider):
    data = provider("L1")

    assert data.inventory == 90.0  # I1 60 + I2 30
    assert [(lot.item_id, lot.quantity, lot.demand_ref) for lot in data.work_in_progress] == [("I1", 30.0, "O3")]
    assert data.pegs == {}


def test_the_same_call_gives_the_same_data_and_a_provider_per_snapshot_date_sees_less(sample, provider):
    assert provider("L1") == provider("L1")

    earlier = SampleLineDataProvider(date(2017, 7, 25), sample)
    assert {o.demand_id for o in earlier("L1").orders} == {"O2"}  # O3는 그때 아직 들어오지 않았다


async def test_a_cycle_over_the_provider_data_is_coordinated_and_validated_against_the_same_files(sample, provider):
    store = make_store(capacity={"L1": 1000.0, "L2": 100.0})
    agent = SupplyCoordinator(store, SETTINGS, provider, provider.line_of)
    for company, item, value in [("A", "I1", 600), ("B", "I1", 300), ("A", "I2", 300), ("A", "I3", 300)]:
        deliver(store, company, item, value)

    results = await pump(agent)
    stock_of = snapshot_stock_provider(sample, pd.Timestamp(AS_OF))
    verdicts = process_pending_validations(store, VALIDATOR, {SUPPLY: allocation_validation_rules(store, VALIDATOR, stock_of)})

    assert {r.outcome for r in results} <= {"coordinated", "idle"}
    assert [v.status for v in verdicts] == ["passed", "passed"] and line_state(store, "L1") == "completed"
    entries = {(e.demand_ref, e.peg): e.quantity for e in store.state.allocations[0].entries}
    assert entries[("O3", "hard")] == 24.0  # 생산 중 물량 30은 고객사 B의 계약 단위 12로 내려 강한 묶임이 된다
    assert entries[("O2", "soft")] == 180.0  # 미납분은 공급이 모자라도 먼저 채운다
    assert sum(entries.values()) <= 1000.0 + 90.0 + 30.0  # 공급 가능량: 잔여 capacity + 재고 + 생산 중 물량
    decided = [e for e in store.negotiation_log(SUPPLY) if e.event == "allocation_decided" and e.payload["line_id"] == "L1"][0]
    assert decided.payload["inputs"]["commitments"][0]["taken_quantity"] == 720.0


@pytest.mark.skipif(not (SAMPLE_DIR / "sample_meta.json").exists(), reason="data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다")
async def test_every_line_of_the_generated_sample_can_be_coordinated_with_its_data():
    from sop.capacity import load_initial_capacity_pools
    from sop.external_data import load_data_end, load_instances

    as_of = load_data_end().date()
    provider = SampleLineDataProvider(as_of)
    pools = load_initial_capacity_pools()
    instances = load_instances()
    pairs = tuple((str(c), str(i)) for c, i in zip(instances["company_id"], instances["item_id"]))
    assert {pool.line_id for pool in pools} == set(provider.lines())

    from sop.coordination_inputs import CoordinationSettings

    store = make_store(instances=pairs, capacity={pool.line_id: pool.total_capacity for pool in pools})
    settings = CoordinationSettings(cycle="2017-08", as_of=as_of, cycle_end=date(2017, 8, 31))
    agent = SupplyCoordinator(store, settings, provider, provider.line_of)
    for company, item in pairs:
        deliver(store, company, item, 6.0)
    await pump(agent)
    notify_cycle_started(store)
    await pump(agent)

    assert all(allocation.cycle == "2017-08" for allocation in store.state.allocations)
    for allocation in store.state.allocations:
        supply = provider(allocation.line_id).inventory + sum(lot.quantity for lot in provider(allocation.line_id).work_in_progress)
        capacity = next(p.remaining_capacity for p in store.state.capacity_pools if p.line_id == allocation.line_id)
        assert sum(e.quantity for e in allocation.entries) <= supply + capacity + 1e-9
