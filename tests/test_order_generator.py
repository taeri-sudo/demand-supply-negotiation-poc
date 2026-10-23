from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from sop.order_generator import (
    CustomerPolicy,
    PolicyChange,
    generate_orders,
    promotion_marks,
)

START = date(2015, 1, 1)
END = date(2015, 12, 31)


def make_pos(seed=0, mean=20.0, days=400, first="2014-06-01", promo_windows=()):
    """일정 수준 + 잡음의 일별 POS. 2014-07-01부터 프로모션이 기록되고 그 전은 기록 없음."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(first, periods=days, freq="D")
    quantity = np.clip(rng.normal(mean, mean * 0.3, days), 0, None).round(0)
    promo = []
    for d in dates:
        if d < pd.Timestamp("2014-07-01"):
            promo.append(None)
        else:
            promo.append(any(a <= d <= b for a, b in promo_windows))
    return pd.DataFrame(
        {"date": dates, "quantity": quantity, "promotion": pd.array(promo, dtype="boolean")}
    )


def policy(**overrides):
    base: dict[str, Any] = dict(company_id="CUST-01", review_days=7, lead_time_days=3, moq=6.0, z=1.28)
    base.update(overrides)
    return CustomerPolicy(**base)


def orders_for(pos=None, pol=None, item="ITEM-1", seed=1):
    return generate_orders(
        pos if pos is not None else make_pos(), pol or policy(), item, START, END, base_seed=seed
    )


def test_same_inputs_and_seed_give_identical_orders_and_other_seed_differs():
    first = orders_for()
    second = orders_for()
    other_seed = orders_for(seed=2)

    pd.testing.assert_frame_equal(first, second)
    assert not first["quantity"].equals(other_seed["quantity"]) or not first["order_date"].equals(
        other_seed["order_date"]
    )


def test_first_order_is_marked_once_on_contract_start_and_is_larger_than_usual():
    """첫 주문은 초기 진열·재고 물량이라 평소 주문보다 크다."""
    orders = orders_for()

    first = orders[orders["is_first_order"]]
    later = orders[~orders["is_first_order"]]
    assert len(first) == 1
    assert first["order_date"].iloc[0] == pd.Timestamp(START)
    assert first["quantity"].iloc[0] > 1.5 * later["quantity"].median()
    assert (orders["order_date"] >= pd.Timestamp(START)).all()


def test_order_quantities_follow_moq_units():
    orders = orders_for(pol=policy(moq=24.0))
    assert (orders["quantity"] % 24.0 == 0).all()
    assert (orders["quantity"] >= 24.0).all()


def test_orders_are_not_a_perfect_mirror_of_pos():
    """점검일 흔들림·건너뜀·수동 조정 때문에 주문 간격이 일정하지 않고 월 주문량이 월 POS와 다르다."""
    pos = make_pos()
    orders = orders_for(pos=pos)

    gaps = orders["order_date"].diff().dropna().dt.days  # pyright: ignore[reportAttributeAccessIssue] -- pandas-stubs가 diff() 결과(Timedelta)의 .dt 접근을 Series[float]로 추론
    assert gaps.nunique() > 2  # 7일 주기가 흔들리고 일부 점검은 건너뜀

    monthly_orders = orders.groupby(orders["order_date"].dt.to_period("M"))["quantity"].sum()
    in_year = pos[(pos["date"] >= "2015-01-01") & (pos["date"] <= "2015-12-31")]
    monthly_pos = in_year.groupby(in_year["date"].dt.to_period("M"))["quantity"].sum()
    assert not np.allclose(monthly_orders.reindex(monthly_pos.index, fill_value=0), monthly_pos)

    # 장기적으로는 판매한 만큼 채우므로 총량은 POS와 크게 어긋나지 않는다
    assert 0.8 < orders["quantity"].sum() / in_year["quantity"].sum() < 1.5


def test_policy_change_alters_only_orders_after_its_effective_date():
    """정책 파라미터의 시기별 변화: 변경일 전 주문은 같고 이후는 달라진다."""
    pos = make_pos()
    plain = orders_for(pos=pos, pol=policy())
    changed = orders_for(
        pos=pos,
        pol=policy(changes=[PolicyChange(effective_from=date(2015, 7, 1), z=2.5, window_days=56)]),
    )

    cut = pd.Timestamp("2015-07-01")
    before_plain = plain[plain["order_date"] < cut].reset_index(drop=True)
    before_changed = changed[changed["order_date"] < cut].reset_index(drop=True)
    pd.testing.assert_frame_equal(before_plain, before_changed)
    after_plain = plain[plain["order_date"] >= cut].reset_index(drop=True)
    after_changed = changed[changed["order_date"] >= cut].reset_index(drop=True)
    assert not after_plain["quantity"].equals(after_changed["quantity"])


def test_item_without_sales_only_gets_the_minimum_first_order():
    pos = make_pos(mean=0.0)
    orders = orders_for(pos=pos, pol=policy(moq=12.0))
    assert len(orders) == 1
    assert orders["quantity"].iloc[0] == 12.0 and orders["is_first_order"].iloc[0]


def test_unrecorded_promotion_period_stays_null_and_is_not_filled_with_false():
    """프로모션 기록이 없는 구간의 주문 표시는 null — false가 아니다."""
    pos = make_pos(first="2014-02-01", days=700)  # 2014-07-01 전은 기록 없음
    pol = policy()
    early = generate_orders(pos, pol, "ITEM-1", date(2014, 3, 1), date(2014, 12, 31), base_seed=1)

    before_record = early[early["order_date"] < "2014-07-01"]
    after_record = early[early["order_date"] >= "2014-07-08"]
    assert len(before_record) > 0 and len(after_record) > 0
    assert before_record["promotion"].isna().all()
    assert after_record["promotion"].notna().all()
    assert (after_record["promotion"] == False).all()  # noqa: E712


def test_promotion_marks_cover_seven_days_after_a_promotion_day():
    pos = make_pos(promo_windows=[(pd.Timestamp("2014-09-10"), pd.Timestamp("2014-09-12"))])
    index = pd.date_range("2014-06-01", "2014-10-01", freq="D")

    marks = promotion_marks(pos, index)

    assert pd.isna(marks[pd.Timestamp("2014-06-15")])  # 기록 시작 전
    assert marks[pd.Timestamp("2014-08-01")] == False  # noqa: E712
    assert marks[pd.Timestamp("2014-09-11")] == True  # noqa: E712
    assert marks[pd.Timestamp("2014-09-18")] == True  # noqa: E712  (마지막 프로모션일 + 6일)
    assert marks[pd.Timestamp("2014-09-20")] == False  # noqa: E712


def test_promotion_raises_order_up_to_level_during_promotion():
    """프로모션 중에는 고객사가 S를 올려 주문이 커진다."""
    promo = [(pd.Timestamp("2015-03-01"), pd.Timestamp("2015-12-31"))]
    base = orders_for(pos=make_pos(promo_windows=[]), pol=policy(moq=1.0))
    lifted = orders_for(pos=make_pos(promo_windows=promo), pol=policy(moq=1.0))

    window = lambda o: o[(o["order_date"] >= "2015-04-01") & ~o["is_first_order"]]["quantity"].sum()
    assert window(lifted) > window(base)


# --- 납기, 수주 식별자, 납품한 양 (M4 수주 생성기 확장) ---------------------------------------------------------------


def test_every_order_has_a_unique_demand_id_and_a_due_date_after_the_lead_time():
    orders = orders_for(pol=policy(lead_time_days=3))

    assert orders["demand_id"].is_unique
    first = orders[orders["is_first_order"]].iloc[0]
    later = orders[~orders["is_first_order"]]
    assert first["due_date"] == first["order_date"]  # 첫 주문은 즉시 입고되는 초기 물량이다
    assert ((later["due_date"] - later["order_date"]).dt.days == 3).all()  # pyright: ignore[reportAttributeAccessIssue]


def test_adding_due_dates_does_not_change_the_orders_themselves():
    """납기 열을 더해도 주문일·수량은 같은 입력과 시드에서 그대로다(같은 난수열을 쓴다)."""
    orders = orders_for()
    again = orders_for(pol=policy(lead_time_days=3))

    pd.testing.assert_frame_equal(orders, again)
    assert list(orders.columns[:7]) == [
        "order_date", "company_id", "item_id", "quantity", "is_first_order", "promotion", "is_synthetic",
    ]


def test_the_customer_counts_orders_in_transit_as_stock_and_does_not_order_the_same_amount_again(monkeypatch):
    """고객사의 재고 정책은 주문했지만 아직 받지 못한 양을 가진 양에 넣는다.

    수요가 일정하고(표준편차 0) 리드타임(6일)이 점검 주기(3일)보다 길면 S = s = 평균 x (리드타임 + 점검 주기)다. 주문마다 주문 직전의
    재고 위치(보유 + 진행 중인 주문)에 주문량을 더하면 정확히 S가 되어야 한다. 진행 중인 주문을 세지 않으면 이미 주문한 양만큼
    더 주문하게 되어 이 합이 S를 넘는다.
    """
    monkeypatch.setattr("sop.order_generator.MANUAL_ADJUSTMENT_PROB", 0.0)  # 수동 조정 주문은 S에 맞춘 수량이 아니다
    daily, lead, review = 10.0, 6, 3
    pos = make_pos(mean=daily, days=500, first="2014-06-01")
    pos["quantity"] = daily  # 변동 없는 수요
    pol = policy(review_days=review, lead_time_days=lead, moq=1.0)
    start = pd.Timestamp("2015-01-01")
    orders = generate_orders(pos, pol, "ITEM-1", start.date(), date(2015, 9, 30), base_seed=3)

    order_up_to = daily * (lead + review)
    first, later = orders[orders["is_first_order"]].iloc[0], orders[~orders["is_first_order"]]
    assert len(later) > 5
    step = pd.Timedelta(lead, unit="D")
    placed: dict[pd.Timestamp, float] = {pd.Timestamp(d): float(q) for d, q in zip(later["order_date"], later["quantity"])}
    arrivals = {day + step: quantity for day, quantity in placed.items()}
    on_hand = float(first["quantity"])
    in_transit: dict[pd.Timestamp, float] = {}
    for day in pd.date_range(start, orders["order_date"].max(), freq="D"):
        if day in arrivals:  # 도착은 점검보다 먼저, 판매는 점검 뒤에 반영된다
            on_hand += arrivals[day]
        in_transit = {a: q for a, q in in_transit.items() if a > day}
        if day in placed:
            assert abs(on_hand + sum(in_transit.values()) + placed[day] - order_up_to) < 1e-9, day
            in_transit[day + step] = placed[day]
        on_hand = max(0.0, on_hand - daily)
    overlapping = [day for day in placed if any(other < day < other + step for other in placed)]
    assert overlapping  # 진행 중인 주문이 있는 상태에서 낸 주문이 실제로 있다


def deliveries_for(orders, moq=6.0, end=None, seed=1):
    from sop.order_generator import simulate_deliveries

    return simulate_deliveries(orders, moq, end or orders["order_date"].max().date(), base_seed=seed)


def test_orders_due_after_the_data_end_are_not_delivered_yet_and_older_ones_are_delivered_in_full():
    orders = orders_for(pol=policy(lead_time_days=3))
    delivered = deliveries_for(orders)  # 기준일은 마지막 주문일이라 마지막 주문의 납기는 기준일 뒤다

    end = orders["order_date"].max()
    future = orders["due_date"] > end
    assert future.any() and (delivered[future] == 0).all()
    old = orders["due_date"] <= end - pd.DateOffset(days=60)
    assert old.any() and (delivered[old] == orders["quantity"][old]).all()


def test_only_recent_orders_are_delivered_short_in_moq_units_and_never_the_first_order():
    shorts = []
    for seed in range(1, 40):  # 확률로 생기는 미납이 어느 시드에서든 최근 구간에만 생기는지 본다
        orders = orders_for(pol=policy(lead_time_days=3, moq=6.0), seed=seed)
        delivered = deliveries_for(orders, moq=6.0, seed=seed)
        end = orders["order_date"].max()
        past_due = orders["due_date"] <= end
        short = (delivered < orders["quantity"]) & past_due
        recent = orders["due_date"] > end - pd.DateOffset(days=60)
        assert (short <= recent).all()  # 오래된 주문에는 미납이 없다
        assert (delivered[short] % 6.0 == 0).all() and (delivered >= 0).all()
        assert not short[orders["is_first_order"]].any()
        shorts.append(int(short.sum()))
    assert sum(shorts) > 0  # 미납이 실제로 생긴다


def test_deliveries_are_reproducible_and_do_not_change_the_orders():
    orders = orders_for()
    before = orders.copy()

    first, second = deliveries_for(orders), deliveries_for(orders)

    pd.testing.assert_series_equal(first, second)
    pd.testing.assert_frame_equal(orders, before)
