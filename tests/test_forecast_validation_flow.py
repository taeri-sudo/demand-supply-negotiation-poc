"""검증agent 공통 틀과 `forecast_validation`: 판정 전달(작성agent 성격별), 판정별 후속, 보류 규칙, edge 일반화, 쓰기 권한.

[테스트 전용 입력] 빠른 테스트는 `assumption_fixtures`의 고정 가정을 쓰고, 실제 forecast 결과를 쓰는 테스트는
`test_forecast_rerun.py`의 합성 입력(연동 근거가 있는 인스턴스)을 쓴다.
"""

import pandas as pd
import pytest

from assumption_fixtures import distinct, fixed_assumptions
from sop import forecast_validation, stats_adapter
from sop.access import PermissionDenied
from sop.escalation_records import ESCALATION_URGENCY, urgency_of
from sop.forecast_human_manager import new_forecast_escalation
from sop.forecast_steps import ForecastRerunHandling
from sop.forecast_supply_allocation import (
    allocate_validated_forecast,
    check_forecast_verdict,
    forecast_supply_protocol_entry,
    forward_validated_forecast,
    process_pending_allocations,
    run_forecast_select_and_submit,
    run_forecast_steps_select_and_submit,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG, monthly_order_history, snapshot_history_provider
from sop.state import (
    Assumption,
    ForecastRecord,
    InteractionProtocol,
    RolePermission,
    Scenario,
    SuspectedCause,
    ValidationResult,
)
from sop.validation_agent import VALIDATION_ERROR_REASON, ValidationRules, process_pending_validations, validate_job
from test_forecast_rerun import AGENT_ID, DEFAULT_ID, TREND_ID, first_run, make
from validation_fixtures import (
    FORECAST,
    SUPPLY,
    VERDICT_CHANNEL,
    forecast_rules,
    forward_passed,
    open_hold,
    write_verdict,
    make_store,
    take_verdict_signals,
    validate_and_allocate,
)

pytestmark = pytest.mark.anyio

RAMEN, SNACK = "COMPANY-A:RAMEN", "COMPANY-A:SNACK"


def two_instance_store(**kwargs):
    return make_store(
        [
            ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN"),
            ForecastRecord(agent_id=SNACK, company_id="COMPANY-A", item_id="SNACK"),
        ],
        **kwargs,
    )


def one_instance_store():
    return make_store([ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")])


async def submit(store, item_id, assumptions=None):
    return await run_forecast_select_and_submit(
        store, FORECAST, "COMPANY-A", item_id, fixed_assumptions(item_id) if assumptions is None else assumptions
    )


def record_of(store, index=0):
    return store.get_field(FORECAST, f"forecast_records[{index}]")


def all_channels_empty(store, *channels):
    return all(store.queue(channel).empty() for channel in channels)


# --- 판정 전달: 항상 작성agent의 validation_result 채널로 push하고 다음 agent에게는 전달하지 않는다 ---------------


async def test_the_validator_pushes_the_verdict_to_the_writer_only_and_not_to_the_next_agent():
    store = two_instance_store()
    await submit(store, "RAMEN")

    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    assert record_of(store).validation.status == "passed"  # 판정은 레코드에 쓴다
    message = store.queue("forecast").get_nowait()
    assert message["record_path"] == "forecast_records[0]" and message["written_by"] == VALIDATOR_ROLE_TAG
    assert all_channels_empty(store, "forecast", SUPPLY, "allocation_candidates")
    assert store.get_field(SUPPLY, "allocation_candidates") == []


async def test_the_forecast_itself_forwards_a_passed_record_to_the_coordinator_once():
    store = two_instance_store()
    await submit(store, "RAMEN")
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False  # 같은 판정을 두 번 보내지 않는다
    (candidate,) = process_pending_allocations(store, SUPPLY)

    assert candidate.allocation == {RAMEN: 120.0}
    assert store.get_field(SUPPLY, "allocation_candidates") == [candidate]


async def test_a_record_that_is_not_passed_is_not_forwarded():
    store = two_instance_store()
    await submit(store, "RAMEN", [Assumption(assumption_id="a", value=9000.0, forecast_uncertainty=5.0)])

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False  # 판정 전
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    assert record_of(store).validation.status == "failed"
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False
    assert store.queue(SUPPLY).empty()


DUMMY_RULES = {"dummy_writer": ValidationRules(instance_id=lambda r: r.agent_id, check=lambda r: [])}


def dummy_store(with_entry: bool):
    entries = [InteractionProtocol(edge="dummy_writer<->dummy_consumer", scope=["dummy_writer", "dummy_consumer"])]
    extra = (RolePermission(role_tag="dummy_writer", field_path="forecast_records", access="w"),)
    store = make_store(
        [ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN")],
        protocol=entries if with_entry else [forecast_supply_protocol_entry()],
        extra_permissions=extra,
    )
    scenario = Scenario(value=1.0, assumption_ids=["a"], derivation="chosen")
    store.set_field("dummy_writer", "forecast_records[0]", record_of(store).model_copy(update={"scenario": scenario}))
    return store  # 기록이 "검증 대기"가 되어 검증agent에게 신호가 갔다(쓴 역할은 dummy_writer)


def test_the_verdict_signals_the_records_author_whoever_wrote_the_record():
    store = dummy_store(with_entry=True)

    results = process_pending_validations(store, VALIDATOR_ROLE_TAG, DUMMY_RULES)

    assert [r.status for r in results] == ["passed"]
    assert store.queue("forecast").qsize() == 1  # 기록의 작성agent(role_tag)의 채널이다
    assert all_channels_empty(store, SUPPLY)  # 다음 agent에게는 가지 않는다


def test_a_failed_verdict_signals_the_same_channel():
    store = dummy_store(with_entry=True)
    rules = {"dummy_writer": ValidationRules(
        instance_id=lambda r: r.agent_id, check=lambda r: [SuspectedCause(type="method_selection")]
    )}

    results = process_pending_validations(store, VALIDATOR_ROLE_TAG, rules)

    assert [r.status for r in results] == ["failed"]
    assert store.queue("forecast").qsize() == 1


def test_an_edge_without_a_protocol_entry_is_rejected():
    store = dummy_store(with_entry=False)

    with pytest.raises(ValueError, match="edge"):
        process_pending_validations(store, VALIDATOR_ROLE_TAG, DUMMY_RULES)


def test_adding_only_a_protocol_entry_makes_a_new_edge_pass_through_the_same_flow():
    assert [r.status for r in process_pending_validations(
        dummy_store(with_entry=True), VALIDATOR_ROLE_TAG, DUMMY_RULES
    )] == ["passed"]


# --- failed: 작성agent(forecast)가 자기 기록의 판정을 확인해 처리한다 --------------------------------------------


async def test_failed_carries_every_cause_and_goes_only_to_the_writer():
    store = two_instance_store()
    ramen = distinct([Assumption(assumption_id="a", value=100.0, forecast_uncertainty=5.0)])
    await submit(store, "RAMEN", ramen + [ramen[0].model_copy(update={"assumption_id": "b", "value": 9000.0})])
    # a와 b는 구성이 같고(not_distinct) b의 값은 범위 밖(value_out_of_range)이다

    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    validation = record_of(store).validation
    assert validation.status == "failed" and validation.validator_role_tag == VALIDATOR_ROLE_TAG
    assert {(c.issue, c.assumption_id) for c in validation.suspected_causes} == {
        ("value_out_of_range", "b"), ("not_distinct", "b"),
    }
    assert all(c.type == "assumption" and c.drivers is None for c in validation.suspected_causes)
    assert store.queue("forecast").qsize() == 1
    assert all_channels_empty(store, SUPPLY)  # 다음 agent에게는 가지 않는다
    assert store.get_field(SUPPLY, "allocation_candidates") == []


def narrow_history_around_default():
    """A-DEFAULT의 값은 범위 안이고 A-TREND의 값은 범위 밖인 과거 월별 요청량."""
    default_value = next(a.value for a in first_run().assumptions if a.assumption_id == DEFAULT_ID)
    assert default_value is not None
    return lambda company_id, item_id: [default_value - 5, default_value + 5]


async def test_flag_rerun_resubmit_and_final_pass_with_real_forecast_results():
    store = one_instance_store()
    inputs = make()
    rules = forecast_rules(narrow_history_around_default())
    result, submitted = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    handling = ForecastRerunHandling(inputs, result)
    assert submitted
    assert store.queue(VERDICT_CHANNEL).empty()  # 아직 판정이 없다

    process_pending_validations(store, VALIDATOR_ROLE_TAG, rules)
    failed = record_of(store).validation
    assert failed.status == "failed"
    assert [(c.issue, c.assumption_id) for c in failed.suspected_causes] == [("value_out_of_range", TREND_ID)]
    assert store.get_field(SUPPLY, "allocation_candidates") == []

    (signal,) = take_verdict_signals(store)  # 판정은 작성agent의 채널로 도착한다
    assert signal["record_path"] == "forecast_records[0]"
    assert await check_forecast_verdict(store, FORECAST, handling) == "rerun"
    after_rerun = record_of(store)
    assert after_rerun.validation is None  # 재제출한 값은 아직 검증받지 않았다
    assert [a.assumption_id for a in after_rerun.assumptions] == [DEFAULT_ID]
    assert [e.assumption_id for e in after_rerun.excluded_assumptions] == [TREND_ID]

    process_pending_validations(store, VALIDATOR_ROLE_TAG, rules)
    assert record_of(store).validation.status == "passed"
    (signal,) = take_verdict_signals(store)
    assert signal["record_path"] == "forecast_records[0]"
    assert await check_forecast_verdict(store, FORECAST, handling) == "forwarded"
    (candidate,) = process_pending_allocations(store, SUPPLY)
    assert candidate.allocation == {AGENT_ID: after_rerun.scenario.value}


async def test_all_causes_of_one_flag_are_handled_in_a_single_rerun():
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda c, i: [1.0, 2.0]))  # 두 가정 모두 범위 밖
    assert {c.assumption_id for c in record_of(store).validation.suspected_causes} == {DEFAULT_ID, TREND_ID}

    assert len(take_verdict_signals(store)) == 1
    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    record = record_of(store)  # 두 이유가 한 번의 재실행으로 모두 처리돼 가정이 모두 제외된다
    assert {e.assumption_id for e in record.excluded_assumptions} == {DEFAULT_ID, TREND_ID}
    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert escalation.reason == "options_exhausted" and escalation.agent_id == AGENT_ID
    assert store.queue(VALIDATOR_ROLE_TAG).empty()  # 보류: 다시 검증agent에 넘기지 않는다


async def test_a_error_record_is_neither_rerun_nor_forwarded():
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[0]", ValidationResult(status="error"))

    assert len(take_verdict_signals(store)) == 1
    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "waiting"
    assert store.queue(SUPPLY).empty()


# --- 쓰기 권한과 push 규칙 -------------------------------------------------------------------------------------


def test_the_validator_can_write_only_the_validation_field_of_any_instance():
    store = two_instance_store()
    verdict = ValidationResult(status="passed")

    write_verdict(store, VALIDATOR_ROLE_TAG, "forecast_records[1]", verdict)  # 인스턴스 인덱스 와일드카드

    assert record_of(store, 1).validation == verdict
    for forbidden in ("forecast_records[0]", "forecast_records[0].scenario", "forecast_records[1].assumptions"):
        with pytest.raises(PermissionDenied):
            store.set_field(VALIDATOR_ROLE_TAG, forbidden, None)
    with pytest.raises(PermissionDenied):
        write_verdict(store, SUPPLY, "forecast_records[0]", verdict)  # 쓰기 권한이 없는 역할


async def test_a_held_instance_record_is_written_without_any_push():
    store = two_instance_store()

    assert await submit(store, "RAMEN", []) is False  # 계산된 가정 없음 → 보류

    assert record_of(store).scenario is None
    assert store.queue(VALIDATOR_ROLE_TAG).empty() and store.queue("forecast").empty()
    assert store.queue("human_manager").qsize() == 1  # 사람을 깨우는 신호만 간다


# --- 검증agent는 State와 이번 주기 스냅샷만 읽는다 -----------------------------------------------------------------


def test_the_history_provider_reads_orders_of_the_snapshot_cut_at_the_snapshot_date(monkeypatch):
    orders = pd.DataFrame(
        {
            "company_id": ["C", "C", "C", "C", "OTHER"],
            "item_id": ["I", "I", "I", "I", "I"],
            "order_date": pd.to_datetime(["2017-05-10", "2017-05-20", "2017-06-10", "2017-07-10", "2017-06-10"]),
            "quantity": [10.0, 5.0, 20.0, 30.0, 999.0],
        }
    )
    monkeypatch.setattr(forecast_validation, "load_orders", lambda data_dir=None: orders)
    monkeypatch.setattr(forecast_validation, "load_data_end", lambda data_dir=None: pd.Timestamp("2017-06-30"))

    history_of = snapshot_history_provider()

    assert history_of("C", "I") == [15.0, 20.0]  # 기준일(6월 말) 뒤 주문과 다른 고객사는 읽지 않는다
    assert snapshot_history_provider(data_end=pd.Timestamp("2017-07-31"))("C", "I") == [15.0, 20.0, 30.0]  # 기준일을 옮기면 따라간다


def test_monthly_order_history_uses_complete_months_only():
    inputs = make()
    history = monthly_order_history(inputs.orders, inputs.data_end)

    assert len(history) == 55 and all(v >= 0 for v in history)  # 2013-01부터 마지막 완전한 달(2017-07)까지


async def test_the_validator_does_not_reproduce_the_forecast_calculation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("검증agent가 통계 계산을 다시 했다")

    monkeypatch.setattr(stats_adapter, "walk_forward_mae", forbidden)
    store = two_instance_store()
    await submit(store, "RAMEN")

    results = process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())

    assert [r.status for r in results] == ["passed"]


# --- error: 재시도 없이 바로 escalation, 그 인스턴스만 멈춘다 -------------------------------------------------


def failing_rules(failures: dict[str, int], calls: dict[str, int]) -> dict[str, ValidationRules]:
    """인스턴스별로 지정한 횟수만큼 예외를 내고 그다음부터 통과하는 규칙."""
    inner = forecast_rules()[FORECAST]

    def check(record):
        calls[record.agent_id] = calls.get(record.agent_id, 0) + 1
        if calls[record.agent_id] <= failures.get(record.agent_id, 0):
            raise RuntimeError("강제로 낸 검증 오류")
        return inner.check(record)

    return {FORECAST: ValidationRules(instance_id=inner.instance_id, check=check)}


async def test_an_error_opens_an_escalation_at_once_and_only_that_instance_stops():
    store = two_instance_store()
    await submit(store, "RAMEN")
    await submit(store, "SNACK")
    calls: dict[str, int] = {}

    process_pending_validations(store, VALIDATOR_ROLE_TAG, failing_rules({RAMEN: 99}, calls))
    forward_passed(store)
    candidates = process_pending_allocations(store, SUPPLY)

    assert calls[RAMEN] == 1  # 재시도하지 않는다
    assert record_of(store, 0).validation.status == "error"
    (escalation,) = store.get_field(FORECAST, "escalation_records")
    assert (escalation.reason, escalation.trigger_edge, escalation.agent_id, escalation.mode, escalation.status) == (
        VALIDATION_ERROR_REASON, f"{VALIDATOR_ROLE_TAG}->human_manager", RAMEN, "intervention", "open",
    )
    assert "RuntimeError" in escalation.rationale
    assert store.queue("human_manager").qsize() == 1
    assert record_of(store, 1).validation.status == "passed"  # 다른 인스턴스는 진행한다
    assert [c.allocation for c in candidates] == [{SNACK: 80.0}]
    assert store.get_field(SUPPLY, "allocation_candidates") == candidates  # 멈춘 인스턴스는 전달되지 않는다


async def test_an_error_is_not_retried_even_when_a_second_run_would_pass():
    store = two_instance_store()
    await submit(store, "RAMEN")
    calls: dict[str, int] = {}

    results = process_pending_validations(store, VALIDATOR_ROLE_TAG, failing_rules({RAMEN: 1}, calls))

    assert calls[RAMEN] == 1 and [r.status for r in results] == ["error"]
    assert len(store.get_field(FORECAST, "escalation_records")) == 1


# --- 처리되지 않은 intervention escalation 기록이 있는 인스턴스는 어디로도 넘기지 않는다 ---------------------


async def test_an_instance_with_an_open_intervention_is_not_sent_to_the_validator():
    store = two_instance_store()
    held = [Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
            Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
            Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5)]  # 가정 선택 ③

    assert await submit(store, "RAMEN", distinct(held)) is False
    assert store.queue(VALIDATOR_ROLE_TAG).empty()
    assert await submit(store, "RAMEN") is False  # 새로 정한 값도 보류한다
    assert store.queue(VALIDATOR_ROLE_TAG).empty()
    assert await submit(store, "SNACK") is True  # 다른 인스턴스는 진행한다
    assert store.queue(VALIDATOR_ROLE_TAG).qsize() == 1


async def test_a_job_that_reaches_the_validator_for_a_held_instance_is_skipped_and_not_forwarded_or_allocated():
    store = two_instance_store()
    await submit(store, "RAMEN")
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    open_hold(store, RAMEN)
    store.queue(VALIDATOR_ROLE_TAG).put_nowait({"field_path": "forecast_records[0]", "written_by": FORECAST})
    before = record_of(store).validation

    assert validate_job(store, VALIDATOR_ROLE_TAG, store.queue(VALIDATOR_ROLE_TAG).get_nowait(), forecast_rules()) is None
    assert record_of(store).validation == before  # 판정하지 않았다
    # 이미 passed인 기록이라도 보류 중이면 forecast는 보내지 않고, 보낸 신호가 있어도 supply_coordination은 만들지 않는다
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False
    assert allocate_validated_forecast(store, SUPPLY, {"field_path": "forecast_records[0]"}) is None


async def test_each_passed_submission_creates_exactly_one_candidate():
    store = two_instance_store()
    await submit(store, "RAMEN")

    assert len(validate_and_allocate(store)) == 1
    assert validate_and_allocate(store) == []  # 같은 판정은 다시 보내지 않는다


# --- escalation 긴급도 대응표 -------------------------------------------------------------------------------


def test_urgency_table_splits_missing_values_and_failures_from_confirmations():
    assert urgency_of("validation_error") == "emergency"
    assert urgency_of("no_computable_assumption") == "emergency"
    assert urgency_of("options_exhausted") == "emergency"
    assert urgency_of("selection_unresolved") == "warning"
    assert urgency_of("commitment_gap") == "warning"
    with pytest.raises(ValueError):
        urgency_of("unknown_reason")


def test_every_reason_the_code_creates_has_an_urgency():
    from typing import get_args

    from sop.forecast_human_manager import ForecastEscalationReason

    created = {VALIDATION_ERROR_REASON, *get_args(ForecastEscalationReason)}
    assert created <= set(ESCALATION_URGENCY)
    assert set(ESCALATION_URGENCY.values()) == {"emergency", "warning"}


# --- 실행 한 번에 기록은 한 번만 쓰고 검증 대기 신호는 최대 한 번 나간다 ------------------------------------------------


@pytest.fixture
def record_writes(monkeypatch):
    """`forecast_records` 레코드(판정 제외)에 쓴 경로를 모두 센다."""
    from sop.access import StateStore

    writes: list[str] = []
    original_commit = StateStore.update_state

    def counting_commit(self, role_tag, **kwargs):
        entries = original_commit(self, role_tag, **kwargs)
        for write in kwargs.get("fields", ()):  # 쓰기가 반영된 뒤에만 센다
            path = write.field_path
            if path.startswith("forecast_records[") and not path.endswith(".validation"):
                writes.append(path)
        return entries

    monkeypatch.setattr(StateStore, "update_state", counting_commit)
    return writes


def pushed(store):
    """forecast가 쓴 기록 때문에 신호가 도착한 채널별 개수(비어 있지 않은 채널만)."""
    names = (VALIDATOR_ROLE_TAG, FORECAST, SUPPLY, VERDICT_CHANNEL)
    return {name: store.queue(name).qsize() for name in names if store.queue(name).qsize()}


async def test_one_run_writes_the_record_once_and_sends_one_validation_job(record_writes):
    store = one_instance_store()
    inputs = make()

    result, submitted = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None

    assert submitted and record_writes == ["forecast_records[0]"]  # 1-4단계 결과와 가정 선택 결과를 한 번에 쓴다
    assert pushed(store) == {VALIDATOR_ROLE_TAG: 1}
    record = record_of(store)
    assert record.scenario is not None and record.excluded_drivers is not None and record.data_sources


async def test_a_rerun_also_writes_once_and_sends_one_job(record_writes):
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(narrow_history_around_default()))
    take_verdict_signals(store)
    record_writes.clear()

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    assert record_writes == ["forecast_records[0]"]
    assert pushed(store) == {VALIDATOR_ROLE_TAG: 1}


async def test_a_run_that_ends_held_sends_no_job_and_pushes_nothing(record_writes):
    store = two_instance_store()
    split = distinct([
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ])  # 가정 선택 ③
    await submit(store, "RAMEN", split)
    await submit(store, "SNACK", [])  # 계산된 가정 없음

    assert record_writes == ["forecast_records[0]", "forecast_records[1]"]  # 실행마다 한 번
    assert pushed(store) == {}  # 검증 대기 신호도, 다른 채널의 신호도 없다
    assert store.queue("human_manager").qsize() == 2  # 사람을 깨우는 신호만 간다


async def test_a_rerun_that_ends_held_writes_once_after_the_escalation_and_sends_no_job(record_writes):
    store = one_instance_store()
    inputs = make()
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules(lambda c, i: [1.0, 2.0]))  # 두 가정 모두 범위 밖
    take_verdict_signals(store)
    record_writes.clear()

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    assert record_writes == ["forecast_records[0]"]  # 전용 쓰기 함수는 escalation 기록이 먼저 있어야만 쓰인다
    assert pushed(store) == {}
    assert [e.reason for e in store.get_field(FORECAST, "escalation_records")] == ["options_exhausted"]
