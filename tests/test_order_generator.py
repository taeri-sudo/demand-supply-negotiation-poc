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
