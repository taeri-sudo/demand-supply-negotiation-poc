"""통계기법 선택 — 후보를 넓게 올리고, 반영할 수 없는 기법만 제외하며, 가정마다 잰 과거 정확도를 기법 가중치로 쓴다."""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sop import forecast_method_selection as fms
from sop import stats_adapter
from sop.data_source_judgment import collect_instance_data
from sop.driver_regressors import Regressors
from sop.external_data import DEFAULT_DATA_DIR, load_instance_inputs, load_instances
from sop.forecast_method_selection import BASE_CANDIDATES, candidate_methods, describe_series, select_methods
from sop.judgment import StructuredJudgment
from sop.judgment_thresholds import (
    HOLDOUT_MONTHS,
    MAX_METHODS_PER_ASSUMPTION,
    METHOD_AMBIGUITY_REL_DIFF,
    MIN_HOLDOUT_MONTHS,
)


def monthly(values, start="2013-01-01"):
    return pd.Series(np.asarray(values, dtype=float), index=pd.date_range(start, periods=len(values), freq="MS"))


def seasonal_series(n=48, noise=0.0, seed=0):
    """매년 같은 모양이 반복되는 계절 시리즈."""
    pattern = np.array([100, 90, 95, 110, 130, 160, 170, 150, 120, 100, 140, 300], dtype=float)
    values = np.tile(pattern, n // 12 + 1)[:n]
    return monthly(values + np.random.default_rng(seed).normal(0, noise, n) if noise else values)


def intermittent_series(n=48, seed=0):
    rng = np.random.default_rng(seed)
    values = np.where(rng.random(n) < 0.25, rng.integers(5, 15, n), 0)
    return monthly(values)


def trend_noise_series(n=48, seed=0):
    rng = np.random.default_rng(seed)
    return monthly(500 + 3 * np.arange(n) + rng.normal(0, 20, n))


def regressors_for(series, seed=1, missing_head=0):
    """[테스트 전용] 임의의 설명변수 한 열. `missing_head`개월은 결측(그 가정이 성립하지 않았던 기간)."""
    column = np.random.default_rng(seed).normal(0, 1, len(series))
    column[:missing_head] = np.nan
    return Regressors(names=["category_trend"], matrix=column[:, None], x_future=np.array([0.3]))


def method_names(result):
    return [m["method"] for m in result.judgment["methods"]]


# --- 후보는 넓게 올라가고, 데이터 특성은 후보를 추가하기만 한다 -----------------------------------


def test_characteristics_only_add_candidates_and_never_remove_the_base_ones():
    plain = candidate_methods(describe_series(trend_noise_series(48)))
    intermittent = candidate_methods(describe_series(intermittent_series()))
    seasonal = candidate_methods(describe_series(seasonal_series()))

    assert plain == BASE_CANDIDATES
    assert set(BASE_CANDIDATES) <= set(intermittent) and "croston_sba" in intermittent
    assert set(BASE_CANDIDATES) <= set(seasonal) and {"ets_seasonal", "seasonal_naive"} <= set(seasonal)
    assert "croston_sba" not in plain and "seasonal_naive" not in plain


# --- 반영할 수 없는 기법만 제외한다 ----------------------------------------------------------------


def test_without_drivers_every_candidate_is_evaluated_and_weights_sum_to_one():
    result = select_methods(trend_noise_series(48))

    assert isinstance(result, StructuredJudgment)  # 공통 규칙 2: {판단값, 근거}
    assert result.judgment["weights"] == "walk_forward"
    assert 1 <= len(result.judgment["methods"]) <= MAX_METHODS_PER_ASSUMPTION
    assert sum(m["method_weight"] for m in result.judgment["methods"]) == pytest.approx(1.0)
    assert result.judgment["holdout_months"] == HOLDOUT_MONTHS


def test_with_drivers_only_methods_that_can_reflect_them_remain():
    series = trend_noise_series(48)
    result = select_methods(series, regressors=regressors_for(series))

    assert set(method_names(result)) <= set(stats_adapter.REGRESSION_METHODS)
    assert result.judgment["regressors"] == ["category_trend"]
    for method in ("ets", "naive", "window_average", "historic_average"):
        assert result.judgment["excluded"][method] == "driver를 반영할 수 없는 기법"


def test_candidates_that_cannot_be_evaluated_are_excluded_with_a_reason(monkeypatch):
    real = stats_adapter.walk_forward_mae
    monkeypatch.setattr(
        fms.stats_adapter, "walk_forward_mae",
        lambda method, y, origins, regressors=None: float("nan") if method == "naive" else real(method, y, origins, regressors),
    )
    result = select_methods(trend_noise_series(48))
    assert "naive" not in method_names(result)
    assert "평가 불가" in result.judgment["excluded"]["naive"]


def test_no_applicable_method_gives_an_empty_selection(monkeypatch):
    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", lambda *a, **k: float("nan"))
    result = select_methods(trend_noise_series(48))
    assert result.judgment["methods"] == [] and result.judgment["exhausted"] is False


# --- 기법 가중치: 가정마다 따로 잰 과거 정확도 -----------------------------------------------------


def patch_errors(monkeypatch, errors):
    monkeypatch.setattr(
        fms.stats_adapter, "walk_forward_mae", lambda method, y, origins, regressors=None: errors[method]
    )


def test_weights_are_inverse_squared_errors_normalised(monkeypatch):
    patch_errors(monkeypatch, {"ets": 10.0, "window_average": 20.0, "historic_average": 40.0,
                               "naive": 80.0, "regression": 160.0, "regression_ar1": 320.0})
    result = select_methods(trend_noise_series(48))
    weights = {m["method"]: m["method_weight"] for m in result.judgment["methods"]}

    raw = {m: 1 / e**2 for m, e in {"ets": 10.0, "window_average": 20.0, "historic_average": 40.0,
                                    "naive": 80.0, "regression": 160.0}.items()}  # 상위 5개만 남는다
    total = sum(raw.values())
    assert set(weights) == set(raw)
    for method, value in raw.items():
        assert weights[method] == pytest.approx(value / total)
    assert "regression_ar1" in result.judgment["excluded"]  # 개수 상한 밖


def test_the_same_series_gets_different_weights_for_different_assumptions(monkeypatch):
    """가정마다 정확도를 따로 재므로, 설명변수가 있는 가정과 없는 가정의 가중치가 다르다."""
    series = trend_noise_series(48)

    def fake(method, y, origins, regressors=None):
        base = {"ets": 10.0, "regression": 30.0, "regression_ar1": 12.0}.get(method, 100.0)
        return base if regressors is None else base * 0.5 if method == "regression" else base * 2

    monkeypatch.setattr(fms.stats_adapter, "walk_forward_mae", fake)
    without = select_methods(series)
    with_driver = select_methods(series, regressors=regressors_for(series))

    w0 = {m["method"]: m["method_weight"] for m in without.judgment["methods"]}
    w1 = {m["method"]: m["method_weight"] for m in with_driver.judgment["methods"]}
    assert w0["regression"] != pytest.approx(w1["regression"])
    assert set(w1) == set(stats_adapter.REGRESSION_METHODS)


def test_top_two_within_ten_percent_is_ambiguous(monkeypatch):
    assert METHOD_AMBIGUITY_REL_DIFF == 0.10
    patch_errors(monkeypatch, {m: 200.0 for m in BASE_CANDIDATES} | {"ets": 100.0, "naive": 110.0})
    assert select_methods(trend_noise_series(48)).ambiguous  # 정확히 10%는 애매함
    patch_errors(monkeypatch, {m: 200.0 for m in BASE_CANDIDATES} | {"ets": 100.0, "naive": 111.0})
    clear = select_methods(trend_noise_series(48))
    assert not clear.ambiguous and clear.ambiguity_reason is None


# --- 표본이 부족하면 균등 가중치와 애매함 -----------------------------------------------------------


def test_too_few_months_where_the_assumption_held_gives_uniform_weights_and_ambiguity():
    series = trend_noise_series(48)
    # 설명변수가 마지막 4개월에만 있었다 — 그 가정이 성립했던 기간이 짧다
    result = select_methods(series, regressors=regressors_for(series, missing_head=44))

    assert result.judgment["holdout_months"] == 4 < MIN_HOLDOUT_MONTHS
    assert result.judgment["weights"] == "uniform"
    weights = [m["method_weight"] for m in result.judgment["methods"]]
    assert weights == [pytest.approx(1 / len(weights))] * len(weights)
    assert result.ambiguous and result.ambiguity_reason is not None
    assert "평가할 달" in result.ambiguity_reason


def test_evaluation_uses_only_months_with_actual_orders_and_driver_data():
    result = select_methods(trend_noise_series(36), n_observed=8)
    assert result.judgment["holdout_months"] == 8  # 보강값이 아닌 실제 주문 달만 평가

    short = select_methods(trend_noise_series(36), n_observed=4)
    assert short.judgment["weights"] == "uniform" and short.ambiguous


def test_exactly_six_months_is_enough_to_evaluate():
    assert MIN_HOLDOUT_MONTHS == 6
    result = select_methods(trend_noise_series(36), n_observed=6)
    assert result.judgment["holdout_months"] == 6 and result.judgment["weights"] == "walk_forward"


# --- 재실행: 직전 기법 제외 -----------------------------------------------------------------------


def test_excluding_a_previous_method_removes_it_from_the_candidates():
    series = seasonal_series(noise=5.0, seed=2)
    first = select_methods(series).judgment["methods"][0]["method"]

    second = select_methods(series, exclude={first})

    assert first not in method_names(second)
    assert second.judgment["excluded"][first] == "재실행에서 제외"


def test_excluding_every_candidate_exhausts_the_choices():
    series = trend_noise_series(48)
    result = select_methods(series, exclude=set(candidate_methods(describe_series(series))))
    assert result.judgment["methods"] == [] and result.judgment["exhausted"] is True


def test_selection_is_deterministic_for_the_same_input():
    series = seasonal_series(noise=5.0, seed=3)
    assert select_methods(series).model_dump() == select_methods(series).model_dump()


# --- 라이브러리 격리 ------------------------------------------------------------------------------


def test_only_the_adapter_module_imports_statsforecast_or_statsmodels():
    src = Path(__file__).resolve().parents[1] / "src" / "sop"
    offenders = []
    for path in src.glob("*.py"):
        if path.name == "stats_adapter.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.split(".")[0] in ("statsforecast", "statsmodels") for n in names):
                offenders.append(path.name)
    assert offenders == []


# --- 실제 샘플 ------------------------------------------------------------------------------------


@pytest.mark.skipif(
    not (DEFAULT_DATA_DIR / "similar_items.csv").exists(),
    reason="data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다",
)
def test_sample_instances_with_different_traits_get_different_candidates_and_weights():
    instances = load_instances()
    results = {}
    for trait in ("seasonal", "sparse", "short_history"):
        row = instances[instances["trait"] == trait].iloc[0]
        collected = collect_instance_data(load_instance_inputs(row["company_id"], row["item_id"]))
        results[trait] = select_methods(collected.training_series, n_observed=collected.n_observed_months)

    assert results["seasonal"].judgment["characteristics"]["strong_seasonal"]
    assert results["sparse"].judgment["characteristics"]["intermittent"]
    for result in results.values():
        assert result.judgment["methods"]
        assert sum(m["method_weight"] for m in result.judgment["methods"]) == pytest.approx(1.0)
    assert len({tuple(method_names(r)) for r in results.values()}) >= 2
