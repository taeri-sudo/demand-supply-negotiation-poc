"""데이터 수집·소스 판단 — 합성 입력으로 각 규칙을 확인하고, 샘플이 있으면 실제 인스턴스로도 확인한다."""

import numpy as np
import pandas as pd
import pytest

from sop.data_source_judgment import apply_to_record, collect_instance_data, last_complete_month
from sop.external_data import DEFAULT_DATA_DIR, InstanceInputs, load_instance_inputs, load_instances
from sop.judgment_thresholds import HISTORY_TARGET_MONTHS, MIN_HISTORY_MONTHS
from sop.state import ForecastRecord

END = pd.Timestamp("2017-08-15")


def pos_frame(start="2013-01-01", mean=50.0, record_start="2014-04-01", bumps=(), seed=0):
    """일별 POS. 프로모션은 record_start부터 기록되고 그 전은 기록 없음(null). bumps는 (시작, 끝, 배수)."""
    days = pd.date_range(start, END, freq="D")
    quantity = np.random.default_rng(seed).poisson(mean, len(days)).astype(float)
    for first, last, factor in bumps:
        quantity[(days >= pd.Timestamp(first)) & (days <= pd.Timestamp(last))] *= factor
    recorded_from = pd.Timestamp(record_start)
    promotion = pd.array([None if d < recorded_from else False for d in days], dtype="boolean")
    return pd.DataFrame({"date": days, "quantity": quantity, "promotion": promotion})


def orders_from_pos(pos, start, ratio=0.9, first_factor=None, promo_months=(), spikes=None):
    """POS 월 합계에 비례하는 월별 주문(한 달에 한 번, 5일). 시작 달의 주문은 첫 주문으로 표시한다."""
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    monthly = pos.groupby(month)["quantity"].sum()
    monthly = monthly[(monthly.index >= pd.Timestamp(start)) & (monthly.index <= pd.Timestamp("2017-07-01"))]
    rows = []
    for i, (m, value) in enumerate(monthly.items()):
        quantity = ratio * value
        if i == 0 and first_factor:
            quantity *= first_factor
        if spikes and m in spikes:
            quantity *= spikes[m]
        rows.append(
            {
                "order_date": m + pd.DateOffset(days=4),
                "quantity": quantity,
                "is_first_order": i == 0,
                "promotion": True if m in promo_months else False,
            }
        )
    frame = pd.DataFrame(rows)
    frame["promotion"] = pd.array(frame["promotion"].tolist(), dtype="boolean")
    return frame


def empty_similar():
    return pd.DataFrame({"date": [], "item_id": [], "quantity": [], "promotion": []})


def category_frame(values, start):
    months = pd.date_range(start, periods=len(values), freq="MS")
    return pd.DataFrame({"month": months, "quantity": values})


def make_inputs(orders, pos, category=None, similar=None, market=None, family="DAIRY"):
    return InstanceInputs(
        company_id="CUST-01",
        item_id="ITEM-1",
        family=family,
        industry="food_processing",
        perishable=False,
        data_end=END,
        orders=orders,
        pos_same=pos,
        pos_similar=similar if similar is not None else empty_similar(),
        pos_category=category if category is not None else pd.DataFrame({"month": [], "quantity": []}),
        market_index=market,
    )


def decisions(result, name):
    return [j for j in result.judgments if j.judgment["decision"] == name]


def test_last_complete_month_excludes_the_month_the_data_ends_in():
    assert last_complete_month(pd.Timestamp("2017-08-15")) == pd.Timestamp("2017-07-01")
    assert last_complete_month(pd.Timestamp("2017-07-31")) == pd.Timestamp("2017-07-01")


# --- 소스: 충분한 인스턴스는 기본 데이터만 -------------------------------------------------


def test_instance_with_enough_history_uses_only_the_base_data():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.0)  # 36개월
    inputs = make_inputs(orders, pos, category_frame(np.arange(40) + 100, "2014-04-01"))

    result = collect_instance_data(inputs)

    assert len(result.training_series) == result.n_observed_months >= MIN_HISTORY_MONTHS
    assert [(s.kind, s.item_scope) for s in result.data_sources] == [("orders", "same_item")]
    assert result.excluded_sources == [] and not result.needs_human
    assert decisions(result, "history_sufficient")


# --- 소스: 이력이 짧으면 보강 -----------------------------------------------------------------


def test_short_history_instance_is_supplemented_from_the_same_item_pos_first():
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    orders = orders_from_pos(pos, "2017-01-01", first_factor=1.0)  # 7개월
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    pos_monthly = pos.groupby(month)["quantity"].sum()  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    category = category_frame((pos_monthly.loc["2013-01-01":"2017-07-01"] * 7).to_numpy(), "2013-01-01")
    inputs = make_inputs(orders, pos, category)

    result = collect_instance_data(inputs)

    assert result.n_observed_months == 7 < MIN_HISTORY_MONTHS
    assert len(result.training_series) == HISTORY_TARGET_MONTHS
    kinds = {(s.kind, s.item_scope) for s in result.data_sources}
    # 같은 item의 POS가 모든 달을 채우므로 상위 단위 데이터(상품군 POS)는 쓰지 않는다
    assert kinds == {("orders", "same_item"), ("pos", "same_item")}
    # 보강한 앞쪽 값은 주문 수준(POS의 0.9배)에 맞춰져 있다
    backcast = result.training_series[result.training_series.index < result.observed_start]
    expected = pos_monthly.reindex(backcast.index) * 0.9
    assert np.allclose(backcast.to_numpy(), expected.to_numpy(), rtol=0.15)
    assert decisions(result, "history_supplemented")


def test_short_history_instance_mixes_available_sources_of_the_same_priority():
    """같은 item POS가 없으면 상위 단위 데이터(상품군 POS, 시장)를 같은 우선순위로 섞어 보강한다."""
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    pos_monthly = pos.groupby(month)["quantity"].sum().loc["2013-01-01":"2017-07-01"]  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    orders = orders_from_pos(pos, "2017-01-01", first_factor=1.0)
    market = pd.Series((pos_monthly * 0.01 + 100).to_numpy(), index=pos_monthly.index)
    inputs = make_inputs(
        orders,
        pos.iloc[0:0],  # 같은 item POS 없음
        category_frame((pos_monthly * 5).to_numpy(), "2013-01-01"),
        market=market,
        family="DAIRY",
    )

    result = collect_instance_data(inputs)

    kinds = {(s.kind, s.item_scope) for s in result.data_sources}
    assert kinds == {("orders", "same_item"), ("pos", "category"), ("market", "category")}
    market_source = next(s for s in result.data_sources if s.kind == "market")
    assert market_source.refs == ["D152"]  # DAIRY의 시장 그룹


def _long_pos_and_category():
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    pos_monthly = pos.groupby(month)["quantity"].sum().loc["2013-01-01":"2017-07-01"]  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    wobble = 1 + 0.1 * np.sin(np.arange(len(pos_monthly)))  # 상품군 POS가 우리 POS와 정확히 비례하지는 않는다
    return pos, category_frame((pos_monthly * 7 * wobble).to_numpy(), "2013-01-01")


def test_backcast_months_use_the_highest_priority_source_that_has_them_and_fall_back_to_the_next():
    pos, category = _long_pos_and_category()
    short_pos = pos[pos["date"] >= pd.Timestamp("2016-01-01")]  # 같은 item POS는 2016-01부터만 있다
    inputs = make_inputs(orders_from_pos(pos, "2017-01-01", first_factor=1.0), short_pos, category)

    result = collect_instance_data(inputs)

    kinds = {(s.kind, s.item_scope) for s in result.data_sources}
    assert kinds == {("orders", "same_item"), ("pos", "same_item"), ("pos", "category")}  # 2015년 이전 달은 상품군 POS
    assert len(result.training_series) == HISTORY_TARGET_MONTHS


def test_each_assumption_is_supplemented_independently_starting_from_its_own_sources():
    pos, category = _long_pos_and_category()
    inputs = make_inputs(orders_from_pos(pos, "2017-01-01", first_factor=1.0), pos, category)

    result = collect_instance_data(inputs, assumption_sources={"A-OWN": frozenset({("pos", "category")}), "A-NONE": frozenset()})

    own, none = result.training_for("A-OWN"), result.training_for("A-NONE")
    assert len(own) == len(none) == HISTORY_TARGET_MONTHS
    assert not np.allclose(own.to_numpy(), none.to_numpy())  # 우선순위가 달라 같은 달의 보강값이 다르다
    assert np.allclose(none.to_numpy(), result.training_series.to_numpy())  # 원인이 없는 가정이 기본 시리즈다
    assert result.training_for("A-UNKNOWN") is result.training_series
    assert {(s.kind, s.item_scope) for s in result.data_sources} >= {("pos", "same_item"), ("pos", "category")}


def test_irrelevant_candidate_goes_to_excluded_sources_and_is_not_used():
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    orders = orders_from_pos(pos, "2017-01-01", first_factor=1.0)
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    pos_monthly = pos.groupby(month)["quantity"].sum().loc["2013-01-01":"2017-07-01"]  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    unrelated = (pos_monthly.max() - pos_monthly).to_numpy() + 100  # 주문과 반대로 움직여 겹치는 기간의 상관이 음수
    inputs = make_inputs(orders, pos, category_frame(unrelated, "2013-01-01"))

    result = collect_instance_data(inputs)

    assert [(e.kind, e.item_scope, e.reason) for e in result.excluded_sources] == [("pos", "category", "irrelevant")]
    assert ("pos", "category") not in {(s.kind, s.item_scope) for s in result.data_sources}
    assert [j.judgment["issue"] for j in decisions(result, "irrelevant_source_excluded")] == ["irrelevant"]


def test_without_any_supplement_data_a_short_history_needs_human():
    """보강할 데이터도 없으면 ③ 사람 escalation."""
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    inputs = make_inputs(orders_from_pos(pos, "2017-01-01", first_factor=1.0), pos.iloc[0:0])

    result = collect_instance_data(inputs)

    assert result.needs_human
    assert result.human_reason is not None
    assert "보강" in result.human_reason
    assert len(result.training_series) == 7
    assert decisions(result, "escalate_no_supplement")


def test_candidate_without_enough_overlap_cannot_be_used():
    pos = pos_frame(start="2013-01-01", record_start="2013-01-01")
    orders = orders_from_pos(pos, "2017-05-01", first_factor=1.0)  # 3개월 — 겹침 기준(4개월) 미만
    month = pos["date"].dt.to_period("M").dt.to_timestamp()
    pos_monthly = pos.groupby(month)["quantity"].sum().loc["2013-01-01":"2017-07-01"]  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    inputs = make_inputs(orders, pos.iloc[0:0], category_frame(pos_monthly.to_numpy(), "2013-01-01"))

    result = collect_instance_data(inputs)

    assert result.needs_human
    assert decisions(result, "supplement_unusable")


# --- 소스: 오염 → cleaning -----------------------------------------------------------------


def test_contaminated_order_month_is_cleaned_and_recorded_in_cleaning():
    pos = pos_frame(start="2014-04-01")
    spike_month = pd.Timestamp("2016-03-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.0, spikes={spike_month: 8.0})

    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))

    assert result.cleaning.applied and result.cleaning.count >= 1
    raw = orders.groupby(orders["order_date"].dt.to_period("M").dt.to_timestamp())["quantity"].sum()  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    assert result.training_series.loc[spike_month] < raw.loc[spike_month] / 3
    assert decisions(result, "cleaning")


def test_recorded_promotion_month_is_not_cleaned_even_if_it_spikes():
    """기록 있는 프로모션은 정제하지 않는다."""
    pos = pos_frame(start="2014-04-01")
    promo_month = pd.Timestamp("2016-03-01")
    orders = orders_from_pos(
        pos, "2014-08-01", first_factor=1.0, promo_months={promo_month}, spikes={promo_month: 8.0}
    )

    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))

    raw = orders.groupby(orders["order_date"].dt.to_period("M").dt.to_timestamp())["quantity"].sum()  # pyright: ignore[reportCallIssue, reportArgumentType] -- pandas-stubs가 DatetimeIndex·Series를 groupby 기준으로 받는 호출 형태를 허용하지 않음
    assert result.training_series.loc[promo_month] == pytest.approx(raw.loc[promo_month])


def test_clean_data_records_no_cleaning():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.0)
    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))
    assert result.cleaning.count == 0 and not result.cleaning.applied


# --- 소스: 무관 → use_from (첫 주문) ----------------------------------------------------------


def test_first_order_far_above_usual_demand_sets_use_from_to_the_next_month():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=3.0)

    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))

    assert result.data_sources[0].use_from == pd.Timestamp("2014-09-01").date()
    assert result.observed_start == pd.Timestamp("2014-09-01")
    assert [j.judgment["issue"] for j in decisions(result, "outdated_first_order_use_from")] == ["outdated"]


def test_first_order_close_to_usual_demand_keeps_all_months():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.1)

    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))

    assert result.data_sources[0].use_from is None
    assert result.observed_start == pd.Timestamp("2014-08-01")


# --- 프로모션 기록 없음 구간 ------------------------------------------------------------------


def test_unlabeled_promotion_that_returns_sets_use_from_after_the_episode():
    """기록 없는 구간에서 한 달 이상 올랐다가 돌아온 구간은 그 구간이 끝난 뒤부터 쓴다."""
    pos = pos_frame(bumps=[("2013-05-06", "2013-06-16", 1.8)])  # 6주
    orders = orders_from_pos(pos, "2013-01-01", first_factor=1.0)

    result = collect_instance_data(make_inputs(orders, pos))

    episodes = [j for j in decisions(result, "promotion_gap_episode") if j.judgment["kind"] == "promotion"]
    assert len(episodes) == 1 and not episodes[0].ambiguous
    use_from = result.data_sources[0].use_from
    assert use_from is not None and pd.Timestamp("2013-06-10") <= pd.Timestamp(use_from) <= pd.Timestamp("2013-06-30")
    assert result.observed_start is not None
    assert result.observed_start >= pd.Timestamp(use_from)
    assert result.observed_start <= pd.Timestamp("2013-08-01")


def test_a_few_days_spike_in_the_unrecorded_period_is_cleaned_without_use_from():
    """며칠 튄 날은 정제하고 use_from은 만들지 않는다."""
    pos = pos_frame(bumps=[("2013-05-06", "2013-05-08", 6.0)])  # 3일
    orders = orders_from_pos(pos, "2013-01-01", first_factor=1.0)

    result = collect_instance_data(make_inputs(orders, pos))

    assert result.cleaning.applied and result.cleaning.count >= 3
    assert result.data_sources[0].use_from is None
    assert not [j for j in decisions(result, "promotion_gap_episode") if j.judgment["kind"] == "promotion"]


def test_rise_that_stays_up_is_kept_as_demand_increase():
    pos = pos_frame(bumps=[("2013-05-06", "2017-08-15", 1.6)])  # 오른 채 계속 유지
    orders = orders_from_pos(pos, "2013-01-01", first_factor=1.0)

    result = collect_instance_data(make_inputs(orders, pos))

    assert result.data_sources[0].use_from is None
    kinds = {j.judgment["kind"] for j in decisions(result, "promotion_gap_episode")}
    assert "promotion" not in kinds
    assert result.observed_start == pd.Timestamp("2013-02-01") or result.observed_start == pd.Timestamp("2013-01-01")


def test_ambiguous_rise_is_marked_but_the_data_is_not_discarded():
    """2주 올랐다 돌아온 구간은 며칠도 한 달도 아니라 애매함 — 표시만 하고 버리지 않는다."""
    pos = pos_frame(bumps=[("2013-05-06", "2013-05-19", 1.8)])
    orders = orders_from_pos(pos, "2013-01-01", first_factor=1.0)

    result = collect_instance_data(make_inputs(orders, pos))

    ambiguous = [j for j in decisions(result, "promotion_gap_episode") if j.ambiguous]
    assert ambiguous and ambiguous[0].ambiguity_reason
    assert result.data_sources[0].use_from is None
    assert len(result.training_series) >= 54


def test_episode_inside_the_recorded_period_is_not_treated_as_unlabeled():
    pos = pos_frame(bumps=[("2015-05-04", "2015-06-14", 1.8)])  # 기록 시작(2014-04) 이후의 상승
    orders = orders_from_pos(pos, "2013-01-01", first_factor=1.0)

    result = collect_instance_data(make_inputs(orders, pos))

    assert not decisions(result, "promotion_gap_episode") or all(
        j.judgment["kind"] == "spike" for j in decisions(result, "promotion_gap_episode")
    )
    assert result.data_sources[0].use_from is None


# --- 레코드 반영 ----------------------------------------------------------------------------


def test_apply_to_record_fills_data_source_fields_without_touching_the_original():
    pos = pos_frame(start="2014-04-01")
    orders = orders_from_pos(pos, "2014-08-01", first_factor=1.0, spikes={pd.Timestamp("2016-03-01"): 8.0})
    result = collect_instance_data(make_inputs(orders, pos.iloc[0:0]))
    record = ForecastRecord(agent_id="CUST-01:ITEM-1", company_id="CUST-01", item_id="ITEM-1")

    updated = apply_to_record(record, result)

    assert record.data_sources == [] and record.cleaning.count == 0
    assert updated.data_sources == result.data_sources
    assert updated.excluded_sources == result.excluded_sources
    assert updated.cleaning.count == result.cleaning.count > 0


# --- 실제 샘플 -------------------------------------------------------------------------------

needs_sample = pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / "similar_items.csv").exists(),
    reason="data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다",
)


@needs_sample
def test_sample_short_history_instances_are_supplemented_and_long_ones_use_base_only():
    instances = load_instances()
    short = instances[instances["trait"] == "short_history"]
    smooth = instances[instances["trait"] == "smooth"]

    for row in short.itertuples():
        result = collect_instance_data(load_instance_inputs(row.company_id, row.item_id))  # pyright: ignore[reportArgumentType] -- itertuples 값이 Scalar로 추론되나 실제는 str
        assert result.n_observed_months < MIN_HISTORY_MONTHS
        assert len(result.training_series) > result.n_observed_months
        assert len(result.data_sources) > 1

    row = smooth.iloc[0]
    result = collect_instance_data(load_instance_inputs(row["company_id"], row["item_id"]))
    assert result.n_observed_months >= MIN_HISTORY_MONTHS
    assert [(s.kind, s.item_scope) for s in result.data_sources] == [("orders", "same_item")]


@needs_sample
def test_sample_unrecorded_period_is_never_filled_with_false_during_collection():
    """수집은 기록 없는 구간을 그대로 두고 판단한다 — 입력의 프로모션 null이 보존돼 있다."""
    inputs = load_instance_inputs("CUST-44", "ITEM-1047679")
    assert inputs.pos_same["promotion"].isna().any()
    assert inputs.pos_same.loc[inputs.pos_same["date"] >= "2014-05-01", "promotion"].notna().all()
