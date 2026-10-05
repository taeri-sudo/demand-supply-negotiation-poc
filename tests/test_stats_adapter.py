"""통계 어댑터 — driver를 설명변수로 받는 회귀 계열 기법과 시계열만 쓰는 기법의 경계."""

import numpy as np
import pytest

from sop import stats_adapter as sa


def regression_data(n=48, beta=5.0, seed=0):
    """[테스트 전용] y = 100 + 계절 + beta × x + 잡음."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 1, n + 1)
    season = 10 * np.sin(2 * np.pi * np.arange(n + 1) / 12)
    y = 100 + season + beta * x + rng.normal(0, 0.5, n + 1)
    return y[:n], y[n], x[:n, None], x[n : n + 1]


@pytest.mark.parametrize("method", sa.REGRESSION_METHODS)
def test_regression_methods_reflect_the_driver_in_their_own_way(method):
    y, actual, x, x_future = regression_data()
    with_driver = sa.forecast_value(method, y, x, x_future)
    shifted = sa.forecast_value(method, y, x, x_future + 2.0)

    assert abs(with_driver - actual) < 4
    assert shifted - with_driver == pytest.approx(10.0, abs=2.5)  # 계수 5 × 변화 2


@pytest.mark.parametrize("method", [m for m in sa.METHODS if m not in sa.REGRESSION_METHODS])
def test_methods_that_use_only_the_series_refuse_drivers(method):
    y, _, x, x_future = regression_data()
    with pytest.raises(ValueError, match="반영할 수 없음"):
        sa.forecast_value(method, y, x, x_future)


def test_regression_needs_the_future_regressor_and_enough_rows():
    y, _, x, x_future = regression_data()
    with pytest.raises(ValueError):
        sa.forecast_value("regression", y, x, None)
    with pytest.raises(ValueError):
        sa.forecast_value("regression", y[:3], x[:3], x_future)
    with pytest.raises(ValueError):
        sa.forecast_value("regression", y, x, np.array([np.nan]))


def test_regression_skips_rows_with_missing_driver_values():
    y, _, x, x_future = regression_data()
    x_missing = x.copy()
    x_missing[:10] = np.nan
    assert np.isfinite(sa.forecast_value("regression", y, x_missing, x_future))
    assert np.isfinite(sa.forecast_value("regression_ar1", y, x_missing, x_future))


def test_season_length_follows_the_method_and_history():
    assert sa.season_length_for("ets", 48) == 1
    assert sa.season_length_for("ets_seasonal", 48) == 12
    assert sa.season_length_for("regression", 48) == 12
    assert sa.season_length_for("regression", 18) == 1  # 이력이 2주기 미만이면 계절 더미를 쓰지 않는다


def test_seasonal_methods_refuse_histories_that_are_too_short():
    short = np.arange(1.0, 10.0)
    with pytest.raises(ValueError):
        sa.forecast_value("seasonal_naive", short)
    with pytest.raises(ValueError):
        sa.forecast_value("ets_seasonal", np.arange(1.0, 20.0))


def test_walk_forward_mae_is_nan_when_any_origin_cannot_be_computed():
    y, _, x, _ = regression_data()
    assert np.isfinite(sa.walk_forward_mae("naive", y, [30, 31, 32]))
    assert np.isnan(sa.walk_forward_mae("naive", y, [30, 31, 32], x))  # naive는 driver를 반영할 수 없다
    assert np.isnan(sa.walk_forward_mae("seasonal_naive", y, [5]))


def test_walk_forward_mae_of_a_driver_model_beats_the_series_only_model():
    y, _, x, _ = regression_data(beta=8.0)
    origins = list(range(30, 48))
    assert sa.walk_forward_mae("regression", y, origins, x) < sa.walk_forward_mae("naive", y, origins)
