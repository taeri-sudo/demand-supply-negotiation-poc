"""`data/generated/`에 커밋된 고정 샘플이 M2 검증에 필요한 특성을 갖는지 확인한다.

원본(data/raw)이 없어도 돌아가야 하므로 이 파일의 테스트는 커밋된 샘플만 읽는다.
"""

import numpy as np
import pandas as pd
import pytest

from sop import external_data
from sop.external_data import (
    DEFAULT_DATA_DIR,
    aggregate_monthly_orders,
    contract_unit,
    load_contract_units,
    load_contracts,
    load_data_end,
    load_instances,
    load_inventory,
    load_line_assignments,
    load_manual_quantities,
    load_min_commitments,
    load_orders,
    load_pos_category_monthly,
    load_pos_daily,
    load_sales_orders,
    load_supply_guarantees,
    load_work_in_progress,
    primary_line,
)
from sop.favorita_mapping import FAMILY_MAPPING, in_scope_families

KEY = ["company_id", "item_id"]


@pytest.fixture(scope="module")
def sample():
    if not (DEFAULT_DATA_DIR / "orders.csv").exists():
        pytest.skip("data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다")
    return {
        "instances": load_instances(),
        "orders": load_orders(),
        "pos": load_pos_daily(),
        "category": load_pos_category_monthly(),
        "contracts": load_contracts(),
        "end": load_data_end(),
    }


def monthly_order_series(orders, key, last_month="2017-07-01"):
    g = aggregate_monthly_orders(orders[(orders["company_id"] == key[0]) & (orders["item_id"] == key[1])], pd.Timestamp("2017-08-15"))
    full = pd.date_range(g["month"].min(), last_month, freq="MS")
    return g.set_index("month")["quantity"].reindex(full, fill_value=0.0)


def seasonal_strength(series):
    q = series.to_numpy(dtype=float)
    t = np.arange(len(q))
    residual = q - np.polyval(np.polyfit(t, q, 1), t)
    months = np.array([m.month for m in series.index])
    explained = sum(
        (months == k).sum() * residual[months == k].mean() ** 2 for k in range(1, 13) if (months == k).any()
    ) / len(q)
    return explained / residual.var()


def test_sample_data_end_is_august_15_2017(sample):
    assert sample["end"] == pd.Timestamp("2017-08-15")


def test_every_instance_has_orders_and_exactly_one_first_order_on_contract_start(sample):
    instances, orders = sample["instances"], sample["orders"]
    assert len(instances) == 16
    for row in instances.itertuples():
        own = orders[(orders["company_id"] == row.company_id) & (orders["item_id"] == row.item_id)]
        first = own[own["is_first_order"]]
        assert len(own) > 0
        assert len(first) == 1, (row.company_id, row.item_id)
        assert first["order_date"].iloc[0] == row.contract_start
        assert own["order_date"].min() == row.contract_start


def test_instances_use_only_in_scope_families_with_matching_industry(sample):
    for row in sample["instances"].itertuples():
        assert row.family in in_scope_families()
        assert row.industry == FAMILY_MAPPING[row.family].industry


def test_orders_follow_each_customers_moq(sample):
    moq = sample["contracts"].set_index("company_id")["moq"]
    orders = sample["orders"]
    assert (orders["quantity"] % orders["company_id"].map(moq) == 0).all()


def test_contracts_cover_every_customer_and_penalty_rates_differ_by_customer(sample):
    assert set(sample["instances"]["company_id"]) == set(sample["contracts"]["company_id"])
    assert sample["contracts"]["shortfall_penalty_rate"].nunique() > 1
    assert sample["contracts"]["moq"].nunique() > 1


def test_order_promotion_is_null_only_before_records_begin_and_never_filled_with_false(sample):
    """주문의 프로모션 표시: 기록이 없는 2014년 4월 이전은 null, 기록이 있는 기간은 true/false."""
    orders = sample["orders"]
    assert orders.loc[orders["order_date"] < "2014-04-01", "promotion"].isna().all()
    assert orders.loc[orders["order_date"] >= "2014-05-01", "promotion"].notna().all()
    after = orders[orders["order_date"] >= "2014-05-01"]["promotion"]
    assert after.eq(True).any() and after.eq(False).any()


def test_pos_promotion_keeps_three_states(sample):
    pos = sample["pos"]
    assert pos.loc[pos["date"] < "2014-04-01", "promotion"].isna().all()
    assert pos.loc[pos["date"] >= "2014-05-01", "promotion"].notna().all()
    assert pos["promotion"].eq(True).any() and pos["promotion"].eq(False).any()


def test_pos_has_target_and_similar_roles_and_instances_have_pos(sample):
    pos = sample["pos"]
    assert set(pos["role"]) == {"target", "similar"}
    targets = pos[pos["role"] == "target"][KEY].drop_duplicates()
    instances = sample["instances"][KEY]
    assert len(targets.merge(instances, on=KEY)) == len(instances)


def test_monthly_sums_exclude_the_last_incomplete_month(sample):
    """Favorita는 2017년 8월 중순에 끝나므로 월별 합산은 2017년 7월까지다."""
    monthly_orders = aggregate_monthly_orders(sample["orders"], sample["end"])
    assert monthly_orders["month"].max() == pd.Timestamp("2017-07-01")
    assert sample["orders"]["order_date"].max() > pd.Timestamp("2017-08-01")  # 8월 주문은 있으나 합산에서 빠짐
    assert sample["category"]["month"].max() == pd.Timestamp("2017-07-01")


def test_category_monthly_covers_only_in_scope_families(sample):
    assert set(sample["category"]["family"]) <= set(in_scope_families())
    assert {"quantity", "sales_days", "promo_days", "nonpromo_days", "unrecorded_days"} <= set(sample["category"].columns)


def test_category_monthly_marks_unrecorded_promotion_period_separately(sample):
    category = sample["category"]
    early = category[category["month"] < "2014-04-01"]
    late = category[category["month"] >= "2014-05-01"]
    assert (early["unrecorded_days"] > 0).all() and (early["promo_days"] == 0).all()
    assert (late["unrecorded_days"] == 0).all()


def test_sample_contains_the_traits_needed_to_validate_method_selection(sample):
    """예측기법 선택 검증에 필요한 특성(계절성, 간헐수요, 짧은 이력)이 실제 샘플에 있어 합성 시리즈를 따로 두지 않는다."""
    instances = sample["instances"]
    orders = sample["orders"]
    series = {
        (r.company_id, r.item_id): monthly_order_series(orders, (r.company_id, r.item_id))
        for r in instances.itertuples()
    }
    seasonal = [k for k, s in series.items() if len(s) >= 48 and seasonal_strength(s) > 0.5]
    intermittent = [k for k, s in series.items() if len(s) >= 48 and (s == 0).sum() >= 5]
    short = [k for k, s in series.items() if len(s) <= 12]

    assert seasonal and intermittent and short
    assert not sample["orders"]["is_synthetic"].any()  # 합성 시리즈가 필요 없어 추가하지 않았다
    assert not sample["instances"]["is_synthetic"].any()


def test_short_history_instances_start_late_but_keep_longer_pos_history(sample):
    pos = sample["pos"]
    short = sample["instances"][sample["instances"]["trait"] == "short_history"]
    assert len(short) == 3
    for row in short.itertuples():
        assert row.contract_start >= pd.Timestamp("2016-10-01")
        own_pos = pos[(pos["company_id"] == row.company_id) & (pos["item_id"] == row.item_id)]
        assert own_pos["date"].min() + pd.DateOffset(years=1) < row.contract_start  # 계약 전 POS가 보강 소스가 된다


def test_orders_are_never_after_the_data_end(sample):
    assert sample["orders"]["order_date"].max() <= sample["end"]


def test_generator_internal_policy_is_not_exposed_through_the_interface():
    """현실의 공급사도 고객사의 재고 정책을 모른다 — 읽는 함수를 두지 않는다."""
    public = [name for name in dir(external_data) if name.startswith("load_")]
    assert not any("polic" in name or "generator" in name for name in public)


# --- supply_coordination이 읽는 입력 (M4) ------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def supply(sample):
    return {
        **sample,
        "orders": load_sales_orders(),
        "manual": load_manual_quantities(),
        "commitments": load_min_commitments(),
        "guarantees": load_supply_guarantees(),
        "units": load_contract_units(),
        "inventory": load_inventory(),
        "wip": load_work_in_progress(),
        "lines": load_line_assignments(),
    }


def test_sales_orders_carry_due_dates_and_delivered_quantities_and_never_exceed_the_order(supply):
    orders, end = supply["orders"], supply["end"]
    assert list(orders.columns) == [
        "demand_id", "company_id", "item_id", "quantity", "order_date", "due_date", "delivered_quantity",
    ]
    assert orders["demand_id"].is_unique
    assert (orders["due_date"] >= orders["order_date"]).all() and (orders["order_date"] <= end).all()
    assert ((orders["delivered_quantity"] >= 0) & (orders["delivered_quantity"] <= orders["quantity"])).all()


def test_the_forecast_order_history_and_the_sales_orders_are_the_same_rows(supply):
    history = load_orders()
    assert len(history) == len(supply["orders"])
    assert (history["demand_id"].to_numpy() == supply["orders"]["demand_id"].to_numpy()).all()


def test_the_sample_has_orders_due_after_the_data_end_and_overdue_backlog(supply):
    orders, end = supply["orders"], supply["end"]
    upcoming = orders[orders["due_date"] > end]
    assert len(upcoming) > 0 and (upcoming["order_date"] <= end).all() and (upcoming["delivered_quantity"] == 0).all()
    backlog = orders["quantity"] - orders["delivered_quantity"]
    overdue = orders[(backlog > 0) & (orders["due_date"] <= end)]
    assert len(overdue) > 0
    assert (overdue["due_date"] > end - pd.DateOffset(days=60)).all()  # 오래된 미납은 정산된 것으로 본다
    old = orders[orders["due_date"] <= end - pd.DateOffset(days=60)]
    assert (old["delivered_quantity"] == old["quantity"]).all()


def test_every_order_quantity_follows_the_contract_unit(supply):
    units = supply["units"]
    for row in supply["orders"].itertuples():
        unit = contract_unit(units, str(row.company_id), str(row.item_id))
        assert row.quantity % unit == 0 and row.delivered_quantity % unit == 0


def test_contract_unit_prefers_the_customer_override_over_the_item_default(supply):
    units = supply["units"]
    assert contract_unit(units, "CUST-34", "ITEM-1047679") == 24.0  # 고객사별 덮어쓰기
    assert contract_unit(units, "CUST-NEW-01", "ITEM-1047679") == 6.0  # 덮어쓰기가 없으면 상품별 기본값
    assert contract_unit(units, None, "ITEM-1047679") == 6.0
    with pytest.raises(KeyError):
        contract_unit(units, None, "ITEM-UNKNOWN")


def test_manual_quantities_cover_the_three_reasons_and_may_have_no_customer(supply):
    manual, units = supply["manual"], supply["units"]
    assert set(manual["input_reason"]) == {"new_product", "new_customer", "promotion"}
    assert (manual["source"] == "manual").all() and manual["demand_id"].is_unique
    assert manual.loc[manual["input_reason"] == "new_product", "company_id"].isna().all()  # 고객사가 정해지지 않은 수량
    assert manual.loc[manual["input_reason"] != "new_product", "company_id"].notna().all()
    for row in manual.itertuples():
        company = None if pd.isna(row.company_id) else str(row.company_id)
        assert row.quantity > 0 and row.quantity % contract_unit(units, company, str(row.item_id)) == 0
        assert primary_line(supply["lines"], str(row.item_id), supply["end"])  # 라인을 찾을 수 있어야 배분에 들어간다


def test_new_product_has_no_order_history_and_new_customer_has_no_orders(supply):
    manual = supply["manual"]
    new_product = manual.loc[manual["input_reason"] == "new_product", "item_id"].iloc[0]
    new_customer = manual.loc[manual["input_reason"] == "new_customer", "company_id"].iloc[0]
    assert new_product not in set(supply["orders"]["item_id"])
    assert new_customer not in set(supply["orders"]["company_id"])


def test_commitments_include_binding_and_non_binding_and_cover_the_data_end(supply):
    commitments, end = supply["commitments"], supply["end"]
    assert set(commitments["binding"]) == {True, False}
    assert ((commitments["period_start"] <= end) & (commitments["period_end"] >= end)).all()
    assert (commitments["total_quantity"] > 0).all()
    instances = {(r.company_id, r.item_id) for r in supply["instances"].itertuples()}
    assert {(r.company_id, r.item_id) for r in commitments.itertuples()} <= instances
    for row in commitments.itertuples():
        assert row.total_quantity % contract_unit(supply["units"], str(row.company_id), str(row.item_id)) == 0


def test_supply_guarantees_are_positive_unit_multiples(supply):
    guarantees = supply["guarantees"]
    assert len(guarantees) > 0 and (guarantees["quantity"] > 0).all()
    for row in guarantees.itertuples():
        assert row.quantity % contract_unit(supply["units"], str(row.company_id), str(row.item_id)) == 0


def test_work_in_progress_is_tied_to_an_upcoming_order_and_is_part_of_it(supply):
    orders = supply["orders"].set_index("demand_id")
    wip = supply["wip"]
    assert len(wip) > 0
    for row in wip.itertuples():
        order = orders.loc[row.demand_ref]
        assert order["due_date"] > supply["end"] and order["item_id"] == row.item_id
        assert 0 < row.quantity <= order["quantity"]


def test_inventory_covers_every_item_with_a_line(supply):
    assert set(supply["lines"]["item_id"]) == set(supply["inventory"]["item_id"])
    assert (supply["inventory"]["quantity"] >= 0).all()


def test_every_item_has_exactly_one_primary_line_and_alternates_are_not_returned(supply):
    lines, end = supply["lines"], supply["end"]
    for item in set(lines["item_id"]):
        assert len(lines[(lines["item_id"] == item) & (lines["preference"] == "primary")]) == 1
    alternates = lines[lines["preference"] == "alternate"]
    assert len(alternates) > 0
    for row in alternates.itertuples():
        assert primary_line(lines, str(row.item_id), end) != row.line_id


def test_the_sample_mixes_lines_shared_by_companies_items_and_a_line_used_alone(supply):
    lines, end = supply["lines"], supply["end"]
    instances = supply["instances"].assign(
        line=[primary_line(lines, item, end) for item in supply["instances"]["item_id"]]
    )
    by_line = instances.groupby("line")
    assert by_line["company_id"].nunique().max() > 1  # 같은 라인을 여러 고객사가 함께 쓴다
    assert (instances.groupby(["line", "company_id"])["item_id"].nunique() > 1).any()  # 같은 회사의 다른 item
    assert (by_line.size() == 1).any()  # 혼자 쓰는 라인


def test_primary_line_uses_the_latest_valid_assignment_not_after_the_date():
    lines = pd.DataFrame(
        {
            "item_id": ["I", "I", "I"],
            "line_id": ["OLD", "NEW", "FUTURE"],
            "preference": ["primary", "primary", "primary"],
            "valid_from": pd.to_datetime(["2013-01-01", "2016-01-01", "2030-01-01"]),
        }
    )
    assert primary_line(lines, "I", pd.Timestamp("2015-06-01")) == "OLD"
    assert primary_line(lines, "I", pd.Timestamp("2017-06-01")) == "NEW"
    with pytest.raises(KeyError):
        primary_line(lines, "I", pd.Timestamp("2012-01-01"))
    with pytest.raises(KeyError):
        primary_line(lines, "OTHER", pd.Timestamp("2017-06-01"))
