import numpy as np
import pandas as pd

from sop.data_cleaning import clean_series

MONTHS = pd.date_range("2014-01-01", periods=48, freq="MS")


def seasonal_monthly(seed=0, level=1000.0, amplitude=600.0, noise=20.0):
    rng = np.random.default_rng(seed)
    values = level + amplitude * np.sin(2 * np.pi * (np.arange(48) % 12) / 12) + rng.normal(0, noise, 48)
    return pd.Series(values, index=MONTHS)


def test_negative_values_are_set_to_zero_and_counted():
    series = pd.Series([5.0, 6.0, -3.0, 5.0, 6.0, 5.0, 6.0, 5.0], index=pd.date_range("2016-01-01", periods=8, freq="D"))

    result = clean_series(series, period=7, k=5.0, fallback_window=5)

    assert result.cleaned.iloc[2] == 0.0
    assert result.n_negative == 1
    assert result.flagged.iloc[2]


def test_outlier_month_is_replaced_by_the_fitted_value_and_counted():
    series = seasonal_monthly()
    series.iloc[30] = series.iloc[30] * 6  # 오류로 크게 튄 달

    result = clean_series(series, period=12, k=4.0, fallback_window=5)

    assert result.flagged.iloc[30]
    assert result.cleaned.iloc[30] < series.iloc[30] * 0.5
    assert abs(result.cleaned.iloc[30] - seasonal_monthly().iloc[30]) < 200  # 같은 달의 정상 수준 근처
    assert result.count == int(result.flagged.sum())
    assert result.n_outliers >= 1


def test_normal_seasonal_peaks_are_not_treated_as_outliers():
    """계절성을 먼저 빼므로 매년 반복되는 큰 피크는 정제되지 않는다."""
    result = clean_series(seasonal_monthly(level=3000.0, amplitude=2500.0), period=12, k=4.0, fallback_window=5)
    assert result.count == 0


def test_protected_promotion_months_are_kept_even_when_they_spike():
    """기록 있는 프로모션은 정상적인 수요 효과라 정제하지 않는다 — 효과 계산이 이 구간에 의존한다."""
    series = seasonal_monthly()
    series.iloc[30] = series.iloc[30] * 6
    protected = pd.Series(False, index=series.index)
    protected.iloc[30] = True

    result = clean_series(series, period=12, k=4.0, fallback_window=5, protected=protected)

    assert not result.flagged.iloc[30]
    assert result.cleaned.iloc[30] == series.iloc[30]


def test_too_short_series_falls_back_to_rolling_median():
    series = pd.Series([10.0, 11.0, 9.0, 10.0, 200.0, 10.0, 11.0, 9.0, 10.0, 11.0], index=pd.date_range("2016-01-01", periods=10, freq="MS"))

    result = clean_series(series, period=12, k=4.0, fallback_window=5)

    assert result.flagged.iloc[4]
    assert result.cleaned.iloc[4] < 20


def test_small_count_values_in_a_sparse_series_are_not_outliers():
    """0이 대부분인 개수 자료에서 1-2개는 정상 변동이다(푸아송 하한)."""
    rng = np.random.default_rng(1)
    counts = rng.poisson(0.3, 200).astype(float)
    series = pd.Series(counts, index=pd.date_range("2016-01-01", periods=200, freq="D"))

    result = clean_series(series, period=7, k=5.0, fallback_window=15)

    assert result.count <= 2


def test_input_series_is_not_modified():
    series = seasonal_monthly()
    series.iloc[30] = series.iloc[30] * 6
    original = series.copy()
    clean_series(series, period=12, k=4.0, fallback_window=5)
    pd.testing.assert_series_equal(series, original)
