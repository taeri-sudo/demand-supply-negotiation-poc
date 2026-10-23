"""사람의 escalation 처리와 시한 초과 처리 (STATE_SCHEMA.md 7번 절).

[테스트 전용 입력] 가정 값은 `assumption_fixtures`의 고정 가정을 쓰고, 계약 단위는 6, 과거 월별 요청량은 [100, 200]이다.
"""

from datetime import datetime, timedelta, timezone

import pytest

from assumption_fixtures import distinct, fixed_assumptions
from sop import access
from sop.access import PermissionDenied
from sop.agent_cards import default_agent_cards
from sop.escalation_records import ESCALATION_REASONS, new_escalation, rule_of
from sop.escalation_resolution import ResolutionRejected, read_escalation_records, resolve_escalation
from sop.coordination_schedule import next_daily_coordination
from sop.forecast_escalation_expiry import expire_due_forecast_escalations, expire_forecast_escalation
from sop.forecast_supply_allocation import (
    check_forecast_verdict,
    forward_validated_forecast,
    passed_times,
    receive_forwarded_forecast,
    run_forecast_select_and_submit,
    send_back_misrouted,
)
from sop.forecast_validation import VALIDATOR_ROLE_TAG
from sop.state import Assumption, ForecastRecord, LogEntry, Scenario, SendBack, SuspectedCause
from sop.validation_agent import ValidationRules, process_pending_validations
from validation_fixtures import FORECAST, HUMAN, SUPPLY, SYSTEM, forecast_rules, make_store, open_hold

pytestmark = pytest.mark.anyio

RAMEN, SNACK = "COMPANY-A:RAMEN", "COMPANY-A:SNACK"
LATER = datetime(2100, 1, 1, tzinfo=timezone.utc)
EARLIER = datetime(2000, 1, 1, tzinfo=timezone.utc)
NOW = datetime(2017, 8, 15, 8, 0, tzinfo=timezone.utc)


def unit_of(company_id, item_id):
    return 6.0


def history_of(company_id, item_id):
    return [100.0, 200.0]


def split_assumptions():
    """값이 갈리고 1등을 가릴 수 없는 가정들 — 가정 선택 ③(`selection_unresolved`), 규칙이 낸 중간값은 110."""
    return distinct([
        Assumption(assumption_id="a", value=100.0, forecast_uncertainty=10.0),
        Assumption(assumption_id="b", value=110.0, forecast_uncertainty=11.0),
        Assumption(assumption_id="c", value=125.0, forecast_uncertainty=12.5),
    ])


def make_pair_store(**kwargs):
    return make_store(
        [
            ForecastRecord(agent_id=RAMEN, company_id="COMPANY-A", item_id="RAMEN"),
            ForecastRecord(agent_id=SNACK, company_id="COMPANY-A", item_id="SNACK"),
        ],
        **kwargs,
    )


async def hold(store, item="RAMEN", kind="no_computable"):
    assumptions = [] if kind == "no_computable" else split_assumptions()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", item, assumptions, "테스트")


def signals(store):
    return {name: queue.qsize() for name, queue in store._queues.items() if queue.qsize()}  # pyright: ignore[reportPrivateUsage]


def drain(store):
    for queue in store._queues.values():  # pyright: ignore[reportPrivateUsage]
        while not queue.empty():
            queue.get_nowait()


def human(store, index=0, value=120.0, **kwargs):
    return resolve_escalation(store, HUMAN, index, kind="value", value=value, unit_of=unit_of, history_of=history_of, **kwargs)


# --- 사람의 처리: 값을 받는 기록과 escalation 기록을 한 번에 쓴다 ---------------------------------------------------------


async def test_a_human_value_for_a_record_without_demand_becomes_the_scenario_and_skips_the_validator():
    store = make_pair_store()
    await hold(store)
    drain(store)

    outcome = human(store, now=NOW)

    escalation = store.state.escalation_records[0]
    assert outcome.escalation == escalation and outcome.warnings == []
    assert (escalation.status, escalation.resolution.kind, escalation.resolution.value, escalation.resolution.resolved_by) == (  # pyright: ignore[reportOptionalMemberAccess]
        "resolved", "value", 120.0, HUMAN,
    )
    assert escalation.resolution.ts == NOW.isoformat()  # pyright: ignore[reportOptionalMemberAccess]
    record = store.state.forecast_records[0]
    assert (record.scenario.value, record.scenario.assumption_ids, record.scenario.derivation, record.selection_basis) == (  # pyright: ignore[reportOptionalMemberAccess]
        120.0, [], None, "human",
    )
    applied = next(e for e in store.negotiation_log(HUMAN) if e.event == "resolution_applied")
    assert applied.payload["previous_scenario"] is None  # 덮어쓰기 전 scenario가 없었다
    assert record.validation is None and record.forward_to is None and record.send_back is None  # 판정 칸은 비워 둔다
    assert store.record_state(FORECAST, "forecast_records[0]") == "judged"
    # 검증agent에게는 신호가 가지 않는다. 작성agent(forecast)는 판정됨 상태와 escalation 기록 처리 신호를 받고, 보류가 끝났음을 다음 agent도 안다
    assert signals(store) == {FORECAST: 2, SUPPLY: 1}


async def test_a_human_value_replaces_the_whole_scenario_and_the_previous_one_stays_in_the_log():
    store = make_pair_store()
    await hold(store, kind="split")
    before = store.state.forecast_records[0].scenario
    assert before is not None and before.derivation == "median" and before.value == 110.0

    human(store, value=126.0)

    record = store.state.forecast_records[0]
    assert record.scenario == Scenario(value=126.0, assumption_ids=[], derivation=None) and record.selection_basis == "human"
    assert [a.assumption_id for a in record.assumptions] == ["a", "b", "c"]  # 가정 목록은 그대로다
    applied = next(e for e in store.negotiation_log(HUMAN) if e.event == "resolution_applied")
    assert applied.payload["previous_scenario"] == before.model_dump(mode="json")


async def test_the_resolution_is_logged_and_the_instance_goes_on_to_the_next_agent_with_the_resolution_time():
    store = make_pair_store()
    await hold(store)
    human(store, now=NOW)
    drain(store)

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True

    record = store.state.forecast_records[0]
    assert record.forward_to == SUPPLY and store.record_state(FORECAST, "forecast_records[0]") == "forwarded"
    assert signals(store) == {SUPPLY: 1}
    applied = next(e for e in store.negotiation_log(FORECAST) if e.event == "resolution_applied")
    assert applied.role_tag == HUMAN and applied.agent_id == RAMEN and applied.payload["escalation"]["status"] == "resolved"
    forwarded = next(e for e in store.negotiation_log(FORECAST) if e.event == "forwarded")
    assert (forwarded.payload["validation_ts"], forwarded.payload["resolution_ts"]) == (None, NOW.isoformat())  # 두 시각 중 하나만 채워진다
    received = receive_forwarded_forecast(store, SUPPLY, store.queue(SUPPLY).get_nowait())
    assert received is not None and received.scenario is not None and received.scenario.value == 120.0
    assert store.queue(VALIDATOR_ROLE_TAG).empty()


async def test_other_instances_are_not_touched_by_a_resolution():
    store = make_pair_store()
    await hold(store, "RAMEN")
    await hold(store, "SNACK")
    snack_before = store.state.forecast_records[1]

    human(store, index=0)

    assert store.state.forecast_records[1] == snack_before and store.state.escalation_records[1].status == "open"


async def test_a_resolution_is_one_state_update_and_applies_nothing_when_it_fails(monkeypatch):
    store = make_pair_store()
    await hold(store)
    before = store.state
    drain(store)

    def broken(*args, **kwargs):
        raise RuntimeError("쓰는 도중 낸 오류")

    monkeypatch.setattr(access, "LogEntry", broken)
    with pytest.raises(RuntimeError, match="쓰는 도중"):
        human(store)

    assert store.state is before and signals(store) == {}


# --- 입력 제한 -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,code",
    [(-6.0, "negative"), (100.0, "not_unit_multiple"), (float("nan"), "not_a_number"), (float("inf"), "not_a_number"),
     (True, "not_a_number"), ("120", "not_a_number"), (None, "not_a_number")],
)
async def test_a_value_that_breaks_an_input_limit_is_rejected_and_nothing_is_written(value, code):
    store = make_pair_store()
    await hold(store)
    before = store.state

    with pytest.raises(ResolutionRejected) as rejected:
        human(store, value=value)

    assert rejected.value.code == code and store.state is before


async def test_zero_is_a_valid_request_quantity():
    store = make_pair_store()
    await hold(store)

    human(store, value=0.0)

    assert store.state.forecast_records[0].scenario.value == 0.0  # pyright: ignore[reportOptionalMemberAccess]


async def test_a_value_far_outside_the_past_range_is_applied_with_a_warning():
    high, low = make_pair_store(), make_pair_store()
    await hold(high)
    await hold(low)

    over = human(high, value=402.0)  # 과거 최대 200의 2배(400)를 넘음
    under = human(low, value=48.0)  # 과거 최소 100의 1/2(50) 아래

    assert len(over.warnings) == 1 and "최대" in over.warnings[0] and len(under.warnings) == 1 and "최소" in under.warnings[0]
    assert high.state.forecast_records[0].scenario.value == 402.0  # pyright: ignore[reportOptionalMemberAccess]
    assert over.warnings[0] in (high.state.escalation_records[0].resolution.note or "")  # pyright: ignore[reportOptionalMemberAccess]
    applied = next(e for e in high.negotiation_log(HUMAN) if e.event == "resolution_applied")
    assert applied.payload["warnings"] == over.warnings


async def test_a_value_inside_the_range_or_without_history_has_no_warning():
    store, no_history = make_pair_store(), make_pair_store()
    await hold(store)
    await hold(no_history)

    assert human(store, value=396.0).warnings == []  # 최대 200의 2배 400 이하
    assert resolve_escalation(no_history, HUMAN, 0, kind="value", value=9000.0, unit_of=unit_of).warnings == []


async def test_the_same_input_twice_is_a_duplicate_and_a_different_one_is_not_open():
    store = make_pair_store()
    await hold(store)
    human(store, value=120.0)
    after_first = store.state

    with pytest.raises(ResolutionRejected) as duplicate:
        human(store, value=120.0)
    with pytest.raises(ResolutionRejected) as processed:
        human(store, value=126.0)

    assert (duplicate.value.code, processed.value.code) == ("duplicate", "not_open") and store.state is after_first


# --- 담당과 처리 종류 ------------------------------------------------------------------------------------------------


async def test_each_role_can_only_resolve_its_own_reasons_and_kinds():
    store = make_pair_store()
    await hold(store)  # human_manager 담당
    open_hold(store, SNACK, "forecast_run_error", "실행 오류")  # system_operator 담당
    before = store.state

    with pytest.raises(ResolutionRejected) as wrong_human:
        resolve_escalation(store, SYSTEM, 0, kind="dismiss")
    with pytest.raises(ResolutionRejected) as wrong_system:
        resolve_escalation(store, HUMAN, 1, kind="value", value=6.0, unit_of=unit_of)
    with pytest.raises(ResolutionRejected) as human_rerun:
        resolve_escalation(store, HUMAN, 0, kind="rerun")
    with pytest.raises(ResolutionRejected) as operator_value:
        resolve_escalation(store, SYSTEM, 1, kind="value", value=6.0)
    with pytest.raises(ResolutionRejected) as operator_with_value:
        resolve_escalation(store, SYSTEM, 1, kind="dismiss", value=6.0)

    assert [e.value.code for e in (wrong_human, wrong_system, human_rerun, operator_value, operator_with_value)] == [
        "wrong_role", "wrong_role", "wrong_kind", "wrong_kind", "wrong_kind",
    ]
    assert store.state is before


async def test_a_notice_cannot_be_resolved():
    store = make_pair_store()
    notice = new_escalation(SUPPLY, "commitment_gap", "알림", line_id="L1")
    store._state = store.state.model_copy(update={"escalation_records": [notice]})  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(ResolutionRejected) as on_notice:
        resolve_escalation(store, HUMAN, 0, kind="value", value=1.0)

    assert on_notice.value.code == "unsupported"


# --- 라인의 수량(commitments_exceed_capacity의 하한 수량, pegged_exceed_capacity의 유지할 강한 묶임 수량) ------------------------------


def floor_store(reason="commitments_exceed_capacity"):
    """라인 L1이 보류된 store. 보류가 난 조율의 로그에는 그 조율의 수요(예측 F-A·F-B, 수주 ORD-1)가 있다."""
    store = make_pair_store()
    demands = {"forecasts": [{"agent_id": "F-A"}, {"agent_id": "F-B"}], "orders": [{"demand_id": "ORD-1"}], "manual": []}
    entry = LogEntry(seq=1, ts=NOW.isoformat(), role_tag=SUPPLY, event="allocation_escalated", payload={"inputs": demands})
    held = new_escalation(SUPPLY, reason, "공급 가능량을 넘음", line_id="L1", log_seq=1)
    store._state = store.state.model_copy(update={"escalation_records": [held], "role_logs": {SUPPLY: [entry]}})  # pyright: ignore[reportPrivateUsage]
    return store


def floors(store, value, **kwargs):
    return resolve_escalation(store, HUMAN, 0, kind="value", value=value, floor_unit_of=lambda ref: 6.0, **kwargs)


@pytest.mark.parametrize("reason", ["commitments_exceed_capacity", "pegged_exceed_capacity"])
def test_line_quantities_are_kept_in_the_resolution_for_the_next_coordination_and_not_written_to_any_record(reason):
    store = floor_store(reason)
    forecast_before = store.state.forecast_records

    floors(store, {"F-A": 300.0, "ORD-1": 120.0}, now=NOW)

    resolution = store.state.escalation_records[0].resolution
    assert resolution is not None and resolution.value == {"F-A": 300.0, "ORD-1": 120.0} and resolution.kind == "value"
    assert store.state.forecast_records == forecast_before


@pytest.mark.parametrize("reason", ["commitments_exceed_capacity", "pegged_exceed_capacity"])
@pytest.mark.parametrize("value", [{}, [], {"F-A": -6.0}, {"F-A": 100.0}, {"F-A": float("nan")}, {"F-A": 300.0, "F-B": 7.0}])
def test_line_quantities_that_break_a_limit_are_rejected(value, reason):
    store = floor_store(reason)
    before = store.state

    with pytest.raises(ResolutionRejected):
        floors(store, value)

    assert store.state is before


@pytest.mark.parametrize("reason", ["commitments_exceed_capacity", "pegged_exceed_capacity"])
def test_a_demand_that_is_not_in_the_coordination_that_raised_the_escalation_is_rejected(reason):
    store = floor_store(reason)
    before = store.state

    with pytest.raises(ResolutionRejected) as error:
        floors(store, {"F-A": 300.0, "F-GONE": 6.0})

    assert error.value.code == "unknown_demand" and "F-GONE" in str(error.value) and store.state is before


# --- 시스템 담당(system_operator)의 처리 ---------------------------------------------------------------------------------


async def test_dismiss_marks_the_escalation_resolved_without_touching_the_record():
    store = make_pair_store()
    open_hold(store, RAMEN, "forecast_run_error", "실행 오류")
    record_before = store.state.forecast_records[0]

    resolve_escalation(store, SYSTEM, 0, kind="dismiss", note="이번 주기에서 뺌", now=NOW)

    escalation = store.state.escalation_records[0]
    assert (escalation.status, escalation.resolution.kind, escalation.resolution.value, escalation.resolution.note) == (  # pyright: ignore[reportOptionalMemberAccess]
        "resolved", "dismiss", None, "이번 주기에서 뺌",
    )
    assert store.state.forecast_records[0] == record_before and store.record_state(FORECAST, "forecast_records[0]") == "none"


async def test_rerun_after_no_next_agent_lets_the_forecast_choose_the_next_agent_again():
    no_known = [c.model_copy(update={"known_agents": []}) if c.role_tag == FORECAST else c for c in default_agent_cards()]
    store = make_pair_store(cards=no_known)
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False
    assert store.record_state(FORECAST, "forecast_records[0]") == "awaiting_system"
    store._state = store.state.model_copy(update={"agent_cards": default_agent_cards()})  # 운영자가 카드를 고친다  # pyright: ignore[reportPrivateUsage]
    drain(store)

    resolve_escalation(store, SYSTEM, 0, kind="rerun")

    assert store.record_state(FORECAST, "forecast_records[0]") == "judged" and signals(store) == {FORECAST: 2, SUPPLY: 1}  # 상태 신호 + 처리 신호, 보류가 끝남
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True


async def test_rerun_after_a_validation_error_clears_the_verdict_so_the_record_is_validated_again():
    store = make_pair_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))

    def broken(record):
        raise RuntimeError("강제로 낸 검증 오류")

    process_pending_validations(store, VALIDATOR_ROLE_TAG, {FORECAST: ValidationRules(lambda r: r.agent_id, broken)})
    assert store.state.forecast_records[0].validation.status == "error"  # pyright: ignore[reportOptionalMemberAccess]
    drain(store)

    resolve_escalation(store, SYSTEM, 0, kind="rerun")

    assert store.state.forecast_records[0].validation is None
    # 검증 오류는 검증agent가 만든 기록이라 카드가 없어 처리 신호는 가지 않는다. 보류가 끝났음은 다음 agent가 안다
    assert store.record_state(FORECAST, "forecast_records[0]") == "awaiting_validation" and signals(store) == {VALIDATOR_ROLE_TAG: 1, SUPPLY: 1}
    (verdict,) = process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    assert verdict.status == "passed"


# --- 담당별 읽기 범위 -----------------------------------------------------------------------------------------------------


async def test_the_human_manager_reads_only_its_own_records_and_the_system_operator_reads_all():
    store = make_pair_store()
    await hold(store, "RAMEN")  # human_manager 담당
    open_hold(store, SNACK, "forecast_run_error", "실행 오류")  # system_operator 담당

    assert [(i, r.reason) for i, r in read_escalation_records(store, HUMAN)] == [(0, "no_computable_assumption")]
    assert [(i, r.reason) for i, r in read_escalation_records(store, SYSTEM)] == [(0, "no_computable_assumption"), (1, "forecast_run_error")]
    with pytest.raises(PermissionDenied):
        read_escalation_records(store, FORECAST)  # 사람이 아닌 역할은 이 함수로 읽지 않는다


# --- 시한과 시한 초과 처리 --------------------------------------------------------------------------------------------------


def test_the_deadline_is_the_next_daily_coordination_time_only_for_reasons_with_an_expiry_action():
    for reason, rule in ESCALATION_REASONS.items():
        record = new_escalation("forecast", reason, "x", agent_id="A", now=NOW)
        if rule.on_expiry is not None and rule.mode == "intervention":
            assert record.deadline == next_daily_coordination(NOW).isoformat() == "2017-08-15T09:00:00+00:00", reason
        else:
            assert record.deadline is None, reason


def test_only_the_human_managers_forecast_reasons_have_an_expiry_action():
    with_expiry = {reason: rule.on_expiry for reason, rule in ESCALATION_REASONS.items() if rule.on_expiry}
    assert with_expiry == {
        "no_computable_assumption": "proceed_without_demand", "selection_unresolved": "proceed_with_value", "options_exhausted": "proceed_with_value",
    }
    assert rule_of("forecast_run_error").on_expiry is None and rule_of("commitments_exceed_capacity").on_expiry is None


async def test_an_expired_selection_proceeds_with_the_rule_value_as_expiry_and_skips_the_validator():
    store = make_pair_store()
    await hold(store, kind="split")
    before = store.state.forecast_records[0]
    drain(store)

    assert expire_forecast_escalation(store, FORECAST, 0, LATER) == "expired_with_value"

    escalation, record = store.state.escalation_records[0], store.state.forecast_records[0]
    assert (escalation.status, escalation.resolution) == ("expired", None)
    assert record.scenario == before.scenario and record.selection_basis == "expiry" and record.validation is None
    assert store.record_state(FORECAST, "forecast_records[0]") == "judged" and signals(store) == {FORECAST: 2, SUPPLY: 1}
    entry = next(e for e in store.negotiation_log(FORECAST) if e.event == "escalation_expired")
    assert entry.agent_id == RAMEN and entry.payload["action"] == "proceed_with_value" and entry.payload["escalation"]["status"] == "expired"


async def test_an_expired_record_is_forwarded_with_the_expiry_time_as_the_resolution_time():
    store = make_pair_store()
    await hold(store, kind="split")
    expire_forecast_escalation(store, FORECAST, 0, LATER)
    drain(store)

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True

    forwarded = next(e for e in store.negotiation_log(FORECAST) if e.event == "forwarded")
    assert (forwarded.payload["validation_ts"], forwarded.payload["resolution_ts"]) == (None, LATER.isoformat())
    assert signals(store) == {SUPPLY: 1}


async def test_an_expired_options_exhausted_proceeds_with_the_value_before_the_rerun():
    store = make_pair_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "RAMEN", fixed_assumptions("RAMEN"))
    kept = store.state.forecast_records[0].scenario
    open_hold(store, RAMEN, "options_exhausted", "재실행으로 가정이 모두 제외됨")

    assert expire_forecast_escalation(store, FORECAST, 0, LATER) == "expired_with_value"

    record = store.state.forecast_records[0]
    assert record.scenario == kept and record.selection_basis == "expiry" and record.validation is None


async def test_an_expired_record_without_computable_assumptions_proceeds_without_demand():
    store = make_pair_store()
    await hold(store)
    before = store.state.forecast_records[0]
    drain(store)

    assert expire_forecast_escalation(store, FORECAST, 0, LATER) == "expired_without_demand"

    record = store.state.forecast_records[0]
    assert store.state.escalation_records[0].status == "expired"
    assert record == before and record.scenario is None and record.selection_basis is None  # 값 없이 진행한다
    # 값이 없는 채로 끝나 담당 채널이 없는 상태가 되지만 작성agent는 처리 신호를, 다음 agent는 보류가 끝났다는 신호를 받는다
    assert store.record_state(FORECAST, "forecast_records[0]") == "none" and signals(store) == {FORECAST: 1, SUPPLY: 1}


async def test_nothing_happens_before_the_deadline_after_it_was_handled_or_for_reasons_without_an_expiry():
    store = make_pair_store()
    await hold(store, "RAMEN", kind="split")
    open_hold(store, SNACK, "forecast_run_error", "실행 오류")
    before = store.state

    assert expire_forecast_escalation(store, FORECAST, 0, EARLIER) == "waiting"  # 시한 전
    assert expire_forecast_escalation(store, FORECAST, 1, LATER) == "waiting"  # system_operator 담당은 시한이 없다
    assert store.state is before and store.state.escalation_records[1].deadline is None
    assert store.record_state(FORECAST, "forecast_records[1]") == "awaiting_system"  # 시한 없이 시스템 대기로 남는다

    human(store, index=0, value=120.0)
    handled = store.state
    assert expire_forecast_escalation(store, FORECAST, 0, LATER) == "waiting"  # 이미 사람이 처리했다
    assert store.state is handled


async def test_expire_due_handles_every_due_record_and_skips_the_rest():
    store = make_pair_store()
    await hold(store, "RAMEN", kind="split")
    await hold(store, "SNACK")
    open_hold(store, "COMPANY-A:OTHER", "forecast_run_error", "x")

    assert expire_due_forecast_escalations(store, FORECAST, LATER) == ["expired_with_value", "expired_without_demand"]
    assert [e.status for e in store.state.escalation_records] == ["expired", "expired", "open"]
    assert expire_due_forecast_escalations(store, FORECAST, LATER) == []


# --- 사람의 처리·시한 초과로 넘어간 기록의 이후 -----------------------------------------------------------------------------


async def test_a_misrouted_return_is_matched_by_the_resolution_time_and_not_mixed_with_the_verdict_time():
    store = make_pair_store()
    await hold(store)
    human(store, value=120.0, now=NOW)
    forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN")
    send_back_misrouted(store, SUPPLY, "forecast_records[0]")
    entry = next(e for e in store.negotiation_log(FORECAST) if e.event == "misrouted_send_back")
    assert (entry.payload["validation_ts"], entry.payload["resolution_ts"]) == (None, NOW.isoformat())  # 두 시각 중 하나만 채워진다
    assert store.record_state(FORECAST, "forecast_records[0]") == "sent_back"

    # 같은 기록을 다시 고르면 반송한 agent가 빠져 후보가 없다
    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is False
    assert store.state.escalation_records[-1].reason == "no_next_agent"


async def test_a_new_resolution_is_a_different_record_so_an_earlier_return_does_not_exclude_the_agent():
    store = make_pair_store()
    await hold(store)
    human(store, value=120.0, now=NOW)
    forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN")
    send_back_misrouted(store, SUPPLY, "forecast_records[0]")
    # 같은 인스턴스가 다시 보류돼 사람이 새 값을 넣는다(처리 시각이 다르다)
    open_hold(store, RAMEN, "selection_unresolved", "다시 확인 필요")
    index = len(store.state.escalation_records) - 1
    human(store, index=index, value=126.0, now=NOW + timedelta(hours=1))

    assert forward_validated_forecast(store, FORECAST, "COMPANY-A", "RAMEN") is True  # 이전 반송은 이 기록에 해당하지 않는다
    assert store.state.forecast_records[0].forward_to == SUPPLY


async def test_the_passed_time_is_the_verdict_time_for_a_passed_record_and_the_resolution_time_for_an_exempt_one():
    store = make_pair_store()
    await run_forecast_select_and_submit(store, FORECAST, "COMPANY-A", "SNACK", fixed_assumptions("SNACK"))
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    passed = store.state.forecast_records[1]
    await hold(store, "RAMEN")
    human(store, value=120.0, now=NOW)

    assert passed_times(store, FORECAST, passed) == {"validation_ts": passed.validation.ts, "resolution_ts": None}  # pyright: ignore[reportOptionalMemberAccess]
    assert passed_times(store, FORECAST, store.state.forecast_records[0]) == {"validation_ts": None, "resolution_ts": NOW.isoformat()}


async def test_a_rerun_after_a_human_value_writes_the_selection_basis_again_and_returns_to_awaiting_validation():
    from sop.forecast_steps import ForecastRerunHandling
    from sop.forecast_supply_allocation import run_forecast_steps_select_and_submit
    from test_forecast_rerun import AGENT_ID, make

    inputs = make()
    store = make_store([ForecastRecord(agent_id=AGENT_ID, company_id="CUST-01", item_id="ITEM-1")])
    result, _ = await run_forecast_steps_select_and_submit(store, FORECAST, inputs)
    assert result is not None
    process_pending_validations(store, VALIDATOR_ROLE_TAG, forecast_rules())
    open_hold(store, AGENT_ID, "options_exhausted", "확인 필요")
    resolve_escalation(
        store, HUMAN, 0, kind="value", value=float(store.state.forecast_records[0].scenario.value // 6 * 6 + 6),  # pyright: ignore[reportOptionalMemberAccess]
        unit_of=unit_of, now=NOW,
    )
    assert store.state.forecast_records[0].selection_basis == "human"
    send_back = SendBack(from_role=SUPPLY, suspected_causes=[SuspectedCause(type="method_selection")], ts="2017-08-16T00:00:00+00:00")
    store.update_state(SUPPLY, fields=[access.FieldWrite("forecast_records[0].send_back", send_back)])
    drain(store)

    assert await check_forecast_verdict(store, FORECAST, ForecastRerunHandling(inputs, result)) == "rerun"

    record = store.state.forecast_records[0]
    assert record.selection_basis == "rule" and record.validation is None  # 사람의 처리로 넘어간 기록도 재실행 뒤에는 다시 검증 대상이다
    assert store.record_state(FORECAST, "forecast_records[0]") == "awaiting_validation" and signals(store) == {VALIDATOR_ROLE_TAG: 1}
