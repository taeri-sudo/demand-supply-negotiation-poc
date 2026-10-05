import pandas as pd
import pytest

from sop.favorita_loader import (
    aggregate_monthly,
    complete_months_only,
    item_to_item_id,
    load_items,
    scan_pos,
    store_to_company_id,
    to_boundary_format,
)


def write_train(path, rows):
    lines = ["id,date,store_nbr,item_nbr,unit_sales,onpromotion"]
    lines += [f"{i},{d},{s},{it},{q},{p}" for i, (d, s, it, q, p) in enumerate(rows)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def daily(rows):
    frame = pd.DataFrame(rows, columns=["date", "quantity", "promotion"])
    frame["date"] = pd.to_datetime(frame["date"])
    frame["promotion"] = pd.array(frame["promotion"].tolist(), dtype="boolean")
    return frame


def test_scan_pos_keeps_only_requested_stores_and_items_in_chunks(tmp_path):
    path = tmp_path / "train.csv"
    write_train(
        path,
        [
            ("2016-01-01", 44, 100, 5.0, "False"),
            ("2016-01-01", 44, 200, 7.0, "False"),  # 다른 상품
            ("2016-01-01", 99, 100, 9.0, "False"),  # 다른 매장
            ("2016-01-02", 44, 100, 6.0, "True"),
        ],
    )

    result = scan_pos(path, {44}, {100}, chunksize=2)

    assert result["quantity"].tolist() == [5.0, 6.0]
    assert set(result["store_nbr"]) == {44} and set(result["item_nbr"]) == {100}


def test_promotion_keeps_three_states_and_never_fills_missing_with_false(tmp_path):
    """기록 없음(빈칸)은 null로 남고 false가 되지 않는다."""
    path = tmp_path / "train.csv"
    write_train(
        path,
        [
            ("2013-05-01", 44, 100, 5.0, ""),  # 프로모션 기록 시작(2014-04) 이전
            ("2014-05-01", 44, 100, 5.0, "False"),
            ("2014-05-02", 44, 100, 5.0, "True"),
        ],
    )

    result = scan_pos(path, {44}, {100})

    assert result["promotion"].isna().tolist() == [True, False, False]
    assert result["promotion"].iloc[1:].tolist() == [False, True]


def test_aggregate_monthly_drops_last_incomplete_month():
    """데이터가 8월 중순에 끝나면 8월은 제외하고 7월까지만 쓴다."""
    frame = daily(
        [
            ("2017-06-30", 1.0, False),
            ("2017-07-01", 2.0, False),
            ("2017-07-31", 3.0, False),
            ("2017-08-01", 4.0, False),
            ("2017-08-15", 5.0, False),
        ]
    ).assign(company_id="CUST-44")

    monthly = aggregate_monthly(frame, ["company_id"])

    assert monthly["month"].tolist() == [pd.Timestamp("2017-06-01"), pd.Timestamp("2017-07-01")]
    assert monthly["quantity"].tolist() == [1.0, 5.0]


def test_aggregate_monthly_keeps_month_when_data_ends_on_month_end():
    frame = daily([("2017-07-01", 2.0, False), ("2017-07-31", 3.0, False)]).assign(company_id="C")
    assert aggregate_monthly(frame, ["company_id"])["month"].tolist() == [pd.Timestamp("2017-07-01")]


def test_aggregate_monthly_uses_explicit_data_end_over_frame_maximum():
    """일부 상품만 읽어 마지막 행이 실제 데이터 끝보다 이르면 data_end를 직접 넘긴다."""
    frame = daily([("2017-07-10", 2.0, False)]).assign(company_id="C")
    assert aggregate_monthly(frame, ["company_id"]).shape[0] == 0
    assert aggregate_monthly(frame, ["company_id"], data_end=pd.Timestamp("2017-08-15")).shape[0] == 1


def test_complete_months_only_filters_by_month_end():
    monthly = pd.DataFrame({"month": pd.to_datetime(["2017-07-01", "2017-08-01"]), "quantity": [1, 2]})
    kept = complete_months_only(monthly, pd.Timestamp("2017-08-15"))
    assert kept["month"].tolist() == [pd.Timestamp("2017-07-01")]


def test_monthly_promotion_status_distinguishes_none_from_unrecorded():
    """기록 없음 구간은 "프로모션 없음"과 구분된다."""
    frame = daily(
        [
            ("2013-05-01", 1.0, None),  # 2013-05: 전부 기록 없음 -> null
            ("2013-05-02", 1.0, None),
            ("2014-05-01", 1.0, False),  # 2014-05: 전부 기록돼 있고 프로모션 없음 -> false
            ("2014-05-02", 1.0, False),
            ("2014-06-01", 1.0, False),  # 2014-06: 프로모션 일 있음 -> true
            ("2014-06-02", 1.0, True),
            ("2014-07-01", 1.0, False),  # 2014-07: 기록 없는 날이 섞여 있어 없음이라 단정 못함 -> null
            ("2014-07-02", 1.0, None),
            ("2014-08-31", 1.0, False),  # 마지막 달을 완전한 달로 만들기 위한 행
        ]
    ).assign(company_id="C")

    monthly = aggregate_monthly(frame, ["company_id"], with_promotion_status=True)
    status = dict(zip(monthly["month"].dt.strftime("%Y-%m"), monthly["promotion"]))

    assert pd.isna(status["2013-05"])
    assert status["2014-05"] == False  # noqa: E712  (null이 아니라 false)
    assert not pd.isna(status["2014-05"])
    assert status["2014-06"] == True  # noqa: E712
    assert pd.isna(status["2014-07"])


def test_monthly_counts_split_promo_nonpromo_and_unrecorded_days():
    frame = daily(
        [
            ("2014-03-30", 1.0, None),
            ("2014-03-31", 1.0, None),
            ("2014-04-01", 1.0, True),
            ("2014-04-02", 1.0, False),
            ("2014-04-30", 1.0, False),
        ]
    ).assign(company_id="C")
    monthly = aggregate_monthly(frame, ["company_id"])
    march = monthly[monthly["month"] == "2014-03-01"].iloc[0]
    april = monthly[monthly["month"] == "2014-04-01"].iloc[0]
    assert (march["unrecorded_days"], march["promo_days"], march["nonpromo_days"]) == (2, 0, 0)
    assert (april["unrecorded_days"], april["promo_days"], april["nonpromo_days"]) == (0, 1, 2)


def test_negative_sales_are_kept_raw_without_cleaning():
    """정제는 데이터 수집과 소스 판단 단계의 몫 — 로더는 반품(음수)을 그대로 둔다."""
    frame = daily([("2016-01-01", -3.0, False), ("2016-01-31", 5.0, False)]).assign(company_id="C")
    assert aggregate_monthly(frame, ["company_id"])["quantity"].tolist() == [2.0]


def test_boundary_format_maps_store_and_item_to_company_and_item_ids():
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(["2016-01-01"]),
            "store_nbr": [44],
            "item_nbr": [103665],
            "quantity": [7.0],
            "promotion": pd.array([None], dtype="boolean"),
        }
    )
    out = to_boundary_format(raw)
    assert out.columns.tolist() == ["date", "company_id", "item_id", "quantity", "promotion"]
    assert out.loc[0, "company_id"] == store_to_company_id(44) == "CUST-44"
    assert out.loc[0, "item_id"] == item_to_item_id(103665) == "ITEM-103665"


def test_load_items_attaches_industry_and_perishable_and_rejects_unmapped_family(tmp_path):
    path = tmp_path / "items.csv"
    path.write_text(
        "item_nbr,family,class,perishable\n1,DAIRY,2000,1\n2,BEVERAGES,1100,0\n3,PET SUPPLIES,9000,0\n",
        encoding="utf-8",
    )
    items = load_items(path).set_index("item_nbr")
    assert items.loc[1, "industry"] == "food_processing" and items.loc[1, "perishable"]
    assert items.loc[2, "industry"] == "beverage_non_alcoholic" and not items.loc[2, "perishable"]
    assert items.loc[3, "industry"] is None and not items.loc[3, "in_scope"]

    bad = tmp_path / "bad.csv"
    bad.write_text("item_nbr,family,class,perishable\n1,NEW FAMILY,1,0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="NEW FAMILY"):
        load_items(bad)
