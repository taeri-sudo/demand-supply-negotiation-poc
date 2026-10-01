import numpy as np
import pandas as pd
import pytest

from pydantic import ValidationError

from sop.market_data import (
    INA_R_SOURCE,
    INDUSTRY_PRICE_INDEX,
    MarketSource,
    deflate_market_index,
    exclude_own_sales,
    own_sales_amount,
)

MONTHS = pd.date_range("2014-01-01", periods=4, freq="MS", name="month")


def test_deflation_leaves_the_original_series_untouched():
    """물가 보정 후에도 원본 매출 지수가 그대로 남고, 보정값은 따로 만들어진다."""
    nominal = pd.Series([100.0, 110.0, 121.0, 133.1], index=MONTHS, name="D151")
    original = nominal.copy()
    price = pd.Series([100.0, 105.0, 110.0, 115.0], index=MONTHS)

    real = deflate_market_index(nominal, price, "2014-01-01")

    pd.testing.assert_series_equal(nominal, original)
    assert real is not nominal
    assert real.name == "D151_real"
    assert not real.equals(nominal)


def test_deflation_divides_by_relative_price_level():
    nominal = pd.Series([100.0, 105.0, 110.0, 115.0], index=MONTHS, name="x")
    price = pd.Series([100.0, 105.0, 110.0, 115.0], index=MONTHS)  # 명목 증가가 전부 물가라면

    real = deflate_market_index(nominal, price, "2014-01-01")

    assert real.tolist() == pytest.approx([100.0] * 4)  # 실질로는 변화 없음


def test_deflation_uses_the_chosen_base_month_level():
    nominal = pd.Series([100.0, 100.0, 100.0, 100.0], index=MONTHS)
    price = pd.Series([100.0, 125.0, 100.0, 100.0], index=MONTHS)

    real = deflate_market_index(nominal, price, "2014-02-01")

    assert real.iloc[1] == pytest.approx(100.0)  # 기준월은 명목과 같다
    assert real.iloc[0] == pytest.approx(125.0)  # 물가가 낮던 달의 명목은 기준월 가격으로 환산하면 더 크다


def test_deflation_keeps_missing_nominal_as_missing():
    nominal = pd.Series([100.0, np.nan, 100.0, 100.0], index=MONTHS)
    price = pd.Series([100.0, 100.0, 100.0, 100.0], index=MONTHS)
    assert np.isnan(deflate_market_index(nominal, price, "2014-01-01").iloc[1])


def test_deflation_raises_instead_of_silently_skipping_missing_price():
    nominal = pd.Series([100.0, 100.0, 100.0, 100.0], index=MONTHS)
    short_price = pd.Series([100.0, 100.0, 100.0], index=MONTHS[:3])
    with pytest.raises(KeyError, match="2014-04"):
        deflate_market_index(nominal, short_price, "2014-01-01")
    with pytest.raises(KeyError, match="기준월"):
        deflate_market_index(nominal, short_price, "2013-01-01")


def test_ina_r_source_is_registered_as_not_including_own_sales():
    """가상 공급사라 INA-R 대형 납세자 매출에 우리 매출이 포함되지 않는다."""
    assert INA_R_SOURCE.includes_own_sales is False


def test_each_industry_has_a_price_index_group():
    assert INDUSTRY_PRICE_INDEX == {
        "food_processing": "011",
        "beverage_non_alcoholic": "012",
        "beverage_alcoholic": "021",
    }


OWN_INCLUDED = MarketSource(name="TEST", includes_own_sales=True, index_unit_amount=1000.0)


def test_own_sales_are_subtracted_when_the_source_includes_them():
    """includes_own_sales가 true이면 우리 매출을 지수 포인트로 환산해 뺀다."""
    nominal = pd.Series([100.0, 110.0, 120.0, 130.0], index=MONTHS, name="D151")
    quantity = pd.Series([2000.0, 5000.0, 0.0, 10000.0], index=MONTHS)
    own = own_sales_amount(quantity, supply_price=1.0)  # 우리 월 매출(공급가 단위)

    ex_own = exclude_own_sales(OWN_INCLUDED, nominal, own)

    # 매출 1000당 지수 1포인트: 2, 5, 0, 10포인트 차감
    assert ex_own.tolist() == pytest.approx([98.0, 105.0, 120.0, 120.0])
    assert ex_own.name == "D151"


def test_exclusion_leaves_the_original_series_untouched():
    nominal = pd.Series([100.0, 110.0, 120.0, 130.0], index=MONTHS)
    original = nominal.copy()
    exclude_own_sales(OWN_INCLUDED, nominal, pd.Series([1000.0] * 4, index=MONTHS))
    pd.testing.assert_series_equal(nominal, original)


def test_nothing_is_subtracted_when_the_source_does_not_include_own_sales():
    """INA-R(includes_own_sales: false)은 우리 매출이 주어져도 그대로 쓴다."""
    nominal = pd.Series([100.0, 110.0, 120.0, 130.0], index=MONTHS)
    own = pd.Series([50000.0] * 4, index=MONTHS)

    result = exclude_own_sales(INA_R_SOURCE, nominal, own)

    pd.testing.assert_series_equal(result, nominal)
    assert result is not nominal


def test_months_without_own_sales_are_left_unchanged():
    nominal = pd.Series([100.0, 110.0, 120.0, 130.0], index=MONTHS)
    own = pd.Series([3000.0], index=MONTHS[1:2])  # 한 달만 우리 매출이 있음
    assert exclude_own_sales(OWN_INCLUDED, nominal, own).tolist() == pytest.approx(
        [100.0, 107.0, 120.0, 130.0]
    )


def test_own_sales_amount_is_quantity_times_supply_price():
    quantity = pd.Series([10.0, 20.0], index=MONTHS[:2])
    assert own_sales_amount(quantity, 1.5).tolist() == [15.0, 30.0]


def test_source_including_own_sales_requires_a_positive_index_unit_amount():
    with pytest.raises(ValidationError):
        MarketSource(name="X", includes_own_sales=True)
    with pytest.raises(ValidationError):
        MarketSource(name="X", includes_own_sales=True, index_unit_amount=0.0)
    MarketSource(name="X", includes_own_sales=False)  # 필요 없음


def test_exclusion_raises_when_own_sales_exceed_the_market():
    """우리 매출이 시장 전체 이상이면 index_unit_amount가 잘못 정해진 것이다."""
    nominal = pd.Series([100.0, 100.0, 100.0, 100.0], index=MONTHS)
    own = pd.Series([0.0, 0.0, 0.0, 200000.0], index=MONTHS)  # 200포인트 > 100
    with pytest.raises(ValueError, match="index_unit_amount"):
        exclude_own_sales(OWN_INCLUDED, nominal, own)


def test_exclusion_then_deflation_keeps_each_step_separate():
    """차감은 명목 지수에, 물가 보정은 그 결과에 한다."""
    nominal = pd.Series([100.0, 110.0, 120.0, 130.0], index=MONTHS, name="D151")
    own = pd.Series([2000.0] * 4, index=MONTHS)
    price = pd.Series([100.0, 105.0, 110.0, 115.0], index=MONTHS)

    ex_own = exclude_own_sales(OWN_INCLUDED, nominal, own)
    real = deflate_market_index(ex_own, price, "2014-01-01")

    assert ex_own.tolist() == pytest.approx([98.0, 108.0, 118.0, 128.0])
    assert real.iloc[0] == pytest.approx(98.0)
    assert real.iloc[1] == pytest.approx(108.0 / 1.05)
