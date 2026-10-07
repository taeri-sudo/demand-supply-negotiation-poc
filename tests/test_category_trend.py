"""category_trend 원인 확인 테스트.

[테스트 전용 입력] 아래 시리즈는 로직 검증을 위해 시장 변화율과 우리 수요 변화율의 관계를 직접
정해 만든 것이다(실데이터가 아니다). `_linked_pair`는 관계가 있는 경우, `_unlinked_pair`는 없는
경우를 만든다.
"""

import numpy as np
import pandas as pd
import pytest

from sop.category_trend import (
    deseasonalized_log_change,
    estimate_category_trend,
    market_change_rate,
)
from sop.judgment import StructuredJudgment
from sop.judgment_thresholds import MARKET_PUBLICATION_LAG_MONTHS, TREND_LAG_MONTHS

N = 72
MONTHS = pd.date_range("2012-01-01", periods=N, freq="MS")
PLANNING = MONTHS[-1]  # 마지막으로 끝난 달 t
SEASON = 1 + 0.2 * np.sin(2 * np.pi * np.arange(N) / 12)


def _levels(log_changes: np.ndarray, start: float) -> pd.Series:
    return pd.Series(start * np.exp(np.cumsum(log_changes)) * SEASON, index=MONTHS)


def _linked_pair(beta: float, noise: float, seed: int = 0) -> tuple[pd.Series, pd.Series]:
    """[테스트 전용] 우리 수요 변화율 = beta × (TREND_LAG_MONTHS개월 전 시장 변화율) + 잡음."""
    rng = np.random.default_rng(seed)
    market_change = rng.normal(0.0, 0.03, N)
    demand_change = rng.normal(0.0, noise, N)
    demand_change[TREND_LAG_MONTHS:] += beta * market_change[:-TREND_LAG_MONTHS]
    return _levels(market_change, 100.0), _levels(demand_change, 1000.0)


def _unlinked_pair(seed: int = 0) -> tuple[pd.Series, pd.Series]:
    """[테스트 전용] 서로 독립인 시장·수요 변화율."""
    rng = np.random.default_rng(seed)
    return _levels(rng.normal(0, 0.03, N), 100.0), _levels(rng.normal(0, 0.03, N), 1000.0)


def test_lag_is_fixed_from_publication_lag_and_horizon():
    """시차는 추정 결과가 아니라 공표 시차(2)와 예측 대상까지의 거리(1)로 정해진다."""
    assert TREND_LAG_MONTHS == MARKET_PUBLICATION_LAG_MONTHS + 1 == 3


def test_deseasonalized_log_change_removes_a_pure_seasonal_pattern():
    level = pd.Series(100.0 * SEASON, index=MONTHS)
    rate = deseasonalized_log_change(level)
    assert rate is not None
    assert np.abs(rate.to_numpy()).max() < 0.02  # 계절 성분만 있으면 변화율이 거의 0


def test_deseasonalized_log_change_returns_none_for_short_or_gapped_series():
    assert deseasonalized_log_change(pd.Series(np.arange(1.0, 20.0), index=MONTHS[:19])) is None
    gapped = pd.Series(100.0 * SEASON, index=MONTHS)
    gapped.iloc[30] = np.nan
    assert deseasonalized_log_change(gapped) is None


def test_market_change_rate_averages_groups_but_keeps_a_single_group_as_is():
    market, other = _linked_pair(0.0, 0.03, seed=5)[0], _unlinked_pair(seed=6)[0]
    single = market_change_rate(market)
    both = market_change_rate(pd.DataFrame({"D151": market, "D152": other}))
    assert single is not None and both is not None
    only_first = market_change_rate(pd.DataFrame({"D151": market}))
    assert only_first is not None
    pd.testing.assert_series_equal(single, only_first, check_names=False)
    expected = (single + market_change_rate(other)) / 2  # type: ignore[operator]
    pd.testing.assert_series_equal(both, expected, check_names=False)


def test_linked_series_keep_the_category_trend_driver_with_the_estimate_as_basis():
    market, demand = _linked_pair(beta=1.0, noise=0.01)
    result = estimate_category_trend(demand, market, PLANNING)

    assert isinstance(result, StructuredJudgment)
    assert result.judgment["decision"] == "significant_effect"
    assert result.judgment["ci_low"] > 0
    assert 0.5 < result.judgment["coefficient"] < 1.5
    assert result.judgment["lag_months"] == TREND_LAG_MONTHS
    assert result.judgment["n"] >= 24
    assert result.judgment["confidence"] == 0.95
    assert "demand_effect" not in result.judgment  # 효과 크기는 통계기법이 요청량으로 계산한다
    for key in ("계수", "신뢰구간", "시차", "n="):
        assert key in result.reasoning


def test_unlinked_series_yield_no_significant_effect():
    market, demand = _unlinked_pair()
    result = estimate_category_trend(demand, market, PLANNING)

    assert result.judgment["decision"] == "no_significant_effect"
    assert "demand_effect" not in result.judgment
    assert result.judgment["ci_low"] <= 0 <= result.judgment["ci_high"]


def test_market_after_the_publication_cutoff_is_not_used():
    """계획 시점에 아직 공표되지 않은 달의 시장 값을 바꿔도 결과가 같다."""
    market, demand = _linked_pair(beta=1.0, noise=0.01)
    base = estimate_category_trend(demand, market, PLANNING)
    tampered = market.copy()
    tampered.iloc[-1] *= 5  # t 달 값(아직 공표 전)
    tampered.iloc[-2] *= 0.2  # t-1 달 값(아직 공표 전)
    assert estimate_category_trend(demand, tampered, PLANNING).judgment == base.judgment


def test_stale_market_series_gives_no_evidence():
    market, demand = _linked_pair(beta=1.0, noise=0.01)
    stale = market.iloc[:-3]  # 가장 최근 시장 달이 t-5
    result = estimate_category_trend(demand, stale, PLANNING)
    assert result.judgment["decision"] == "no_evidence"
    assert "알 수 있는" in result.reasoning


def test_too_short_history_gives_no_evidence():
    market, demand = _linked_pair(beta=1.0, noise=0.01)
    short_demand = demand.iloc[-20:]  # 계절 분해에 필요한 25개월 미만
    result = estimate_category_trend(short_demand, market, PLANNING)
    assert result.judgment["decision"] == "no_evidence"


def test_result_is_deterministic():
    market, demand = _linked_pair(beta=1.0, noise=0.01)
    assert (
        estimate_category_trend(demand, market, PLANNING).judgment
        == estimate_category_trend(demand, market, PLANNING).judgment
    )


def test_ci_barely_excluding_zero_is_marked_ambiguous(monkeypatch):
    """신뢰구간이 0에 가까스로 걸치면 `ambiguous`(경계 처리 로직 검증용 — 추정값을 직접 지정)."""
    from sop import category_trend, stats_adapter

    market, demand = _linked_pair(beta=1.0, noise=0.01)
    barely = stats_adapter.SlopeEstimate(coefficient=0.5, ci_low=0.01, ci_high=1.0, n=60, maxlags=3)
    monkeypatch.setattr(category_trend.stats_adapter, "ols_slope_hac", lambda *a, **k: barely)
    result = estimate_category_trend(demand, market, PLANNING)
    assert result.judgment["decision"] == "significant_effect" and result.ambiguous
    assert result.ambiguity_reason

    clear = stats_adapter.SlopeEstimate(coefficient=0.5, ci_low=0.3, ci_high=0.7, n=60, maxlags=3)
    monkeypatch.setattr(category_trend.stats_adapter, "ols_slope_hac", lambda *a, **k: clear)
    assert not estimate_category_trend(demand, market, PLANNING).ambiguous


def test_false_driver_rate_on_unlinked_series_stays_low():
    """관계가 없는 입력 40쌍에서 원인이 유지되는 비율. 신뢰수준 95%의 명목 비율(5%)보다 다소 높을 수
    있어(표본이 작은 HAC, DESIGN.md "category_trend 원인과 가정별 통계기법" 한계) 상한을 느슨하게 둔다."""
    false_positives = 0
    for seed in range(40):
        market, demand = _unlinked_pair(seed)
        if estimate_category_trend(demand, market, PLANNING).judgment["decision"] == "significant_effect":
            false_positives += 1
    assert false_positives / 40 <= 0.20
