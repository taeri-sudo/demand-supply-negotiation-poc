"""예측기법 선택 — 데이터 특성에 따라 후보가 갈리고, 후보끼리 평가해 하나를 고르며, 애매함을 표시한다."""

import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sop import forecast_method_selection as fms
from sop.external_data import DEFAULT_DATA_DIR, load_instance_inputs, load_instances
from sop.data_source_judgment import collect_instance_data
from sop.forecast_method_selection import candidate_methods, describe_series, select_forecast_method
from sop.judgment import StructuredJudgment
from sop.judgment_thresholds import (
    HOLDOUT_MONTHS,
    METHOD_AMBIGUITY_REL_DIFF,
    METHOD_ERROR_METRIC,
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


# --- 특성 분류와 후보 -------------------------------------------------------------------------


def test_characteristics_classify_seasonal_intermittent_short_and_regular():
    assert describe_series(seasonal_series()).kind == "seasonal"
    assert describe_series(intermittent_series()).kind == "intermittent"
    assert describe_series(trend_noise_series(18)).kind == "short"  # 24개월 미만
    assert describe_series(trend_noise_series(48)).kind == "regular"


def test_candidates_depend_on_characteristics_and_short_history_has_no_seasonal_method():
    kinds = {
        kind: candidate_methods(describe_series(series))
        for kind, series in {
            "seasonal": seasonal_series(),
            "intermittent": intermittent_series(),
            "short": trend_noise_series(18),
            "regular": trend_noise_series(48),
        }.items()
    }
    assert "croston_sba" in kinds["intermittent"] and "croston_sba" not in kinds["regular"]
    assert "seasonal_naive" in kinds["seasonal"]
    assert not {"seasonal_naive"} & set(kinds["short"] + kinds["regular"] + kinds["intermittent"])
    assert len({tuple(v) for v in kinds.values()}) == 4


# --- 선택 ----------------------------------------------------------------------------------------


def test_instances_with_different_data_characteristics_receive_different_methods():
    """하드코딩 스텁과 달리 실제 입력에 반응해 서로 다른 forecast_method를 받는다."""
    seasonal = select_forecast_method(seasonal_series())
    short = select_forecast_method(trend_noise_series(18))
    intermittent = select_forecast_method(intermittent_series())

    methods = {
        seasonal.judgment["forecast_method"],
        short.judgment["forecast_method"],
        intermittent.judgment["forecast_method"],
    }
    assert seasonal.judgment["forecast_method"] == "seasonal_naive"  # 계절 패턴이 정확히 반복되면 오차 0
    assert short.judgment["forecast_method"] in candidate_methods(describe_series(trend_noise_series(18)))
    assert intermittent.judgment["forecast_method"] in candidate_methods(describe_series(intermittent_series()))
    assert len(methods) >= 2
    assert seasonal.judgment["season_length"] == 12 and short.judgment["season_length"] == 1


def test_judgment_reports_error_metric_holdout_and_per_method_errors():
    result = select_forecast_method(seasonal_series(noise=5.0))

    assert isinstance(result, StructuredJudgment)  # 공통 규칙 2: {판단값, 근거}
    assert result.judgment["error_metric"] == METHOD_ERROR_METRIC == "mae"
    assert result.judgment["holdout_months"] == HOLDOUT_MONTHS == 12
    errors = result.judgment["errors"]
    assert set(errors) == set(candidate_methods(describe_series(seasonal_series())))
    best = result.judgment["forecast_method"]
    assert errors[best] == min(errors.values())
    assert result.reasoning


def test_selection_is_deterministic_for_the_same_input():
    series = seasonal_series(noise=5.0, seed=3)
    first, second = select_forecast_method(series), select_forecast_method(series)
    assert first.model_dump() == second.model_dump()


def test_forecast_method_is_a_name_the_adapter_knows():
    from sop.stats_adapter import METHODS

    assert select_forecast_method(trend_noise_series(48)).judgment["forecast_method"] in METHODS


# --- 애매함 -----------------------------------------------------------------------------------


def patch_errors(monkeypatch, errors):
    monkeypatch.setattr(
        fms.stats_adapter, "rolling_one_step_mae", lambda method, y, season_length, n_eval, min_train: errors[method]
    )


def test_top_two_within_ten_percent_is_ambiguous_but_still_chooses_the_lowest(monkeypatch):
    patch_errors(monkeypatch, {"ets": 100.0, "window_average": 109.0, "naive": 200.0})

    result = select_forecast_method(trend_noise_series(48))

    assert result.judgment["forecast_method"] == "ets"
    assert result.ambiguous
    assert result.ambiguity_reason is not None
    assert "ets" in result.ambiguity_reason and "window_average" in result.ambiguity_reason


def test_ambiguity_boundary_is_ten_percent_inclusive(monkeypatch):
    assert METHOD_AMBIGUITY_REL_DIFF == 0.10
    patch_errors(monkeypatch, {"ets": 100.0, "window_average": 110.0, "naive": 200.0})
    assert select_forecast_method(trend_noise_series(48)).ambiguous  # 정확히 10%는 애매함
    patch_errors(monkeypatch, {"ets": 100.0, "window_average": 111.0, "naive": 200.0})
    clear = select_forecast_method(trend_noise_series(48))
    assert not clear.ambiguous and clear.ambiguity_reason is None


def test_equal_errors_are_ambiguous_even_when_both_are_zero(monkeypatch):
    patch_errors(monkeypatch, {"ets": 0.0, "window_average": 0.0, "naive": 5.0})
    assert select_forecast_method(trend_noise_series(48)).ambiguous


def test_clear_winner_is_not_ambiguous(monkeypatch):
    patch_errors(monkeypatch, {"ets": 100.0, "window_average": 200.0, "naive": 300.0})
    result = select_forecast_method(trend_noise_series(48))
    assert result.judgment["forecast_method"] == "ets"
    assert not result.ambiguous


def test_too_short_to_evaluate_falls_back_to_first_candidate_and_is_ambiguous():
    result = select_forecast_method(trend_noise_series(8))
    assert result.judgment["holdout_months"] == 0 and result.judgment["errors"] == {}
    assert result.judgment["forecast_method"] == candidate_methods(describe_series(trend_noise_series(8)))[0]
    assert result.ambiguous
    assert result.ambiguity_reason is not None
    assert "평가" in result.ambiguity_reason


# --- 평가는 실제 주문 구간으로만 -------------------------------------------------------------


def test_evaluation_uses_only_the_months_with_actual_orders(monkeypatch):
    """보강값이 앞쪽에 붙은 시리즈는 평가 구간을 실제 주문 달 수로 줄인다."""
    seen = {}

    def spy(method, y, season_length, n_eval, min_train):
        seen[method] = n_eval
        return 100.0 + len(seen)

    monkeypatch.setattr(fms.stats_adapter, "rolling_one_step_mae", spy)

    result = select_forecast_method(trend_noise_series(36), n_observed=8)

    assert result.judgment["holdout_months"] == 8
    assert set(seen.values()) == {8}


def test_evaluation_window_is_capped_at_twelve_months_when_actual_orders_are_plentiful():
    result = select_forecast_method(trend_noise_series(48), n_observed=40)
    assert result.judgment["holdout_months"] == HOLDOUT_MONTHS == 12


def test_fewer_than_six_actual_months_skips_evaluation_and_marks_ambiguous():
    assert MIN_HOLDOUT_MONTHS == 6
    series = trend_noise_series(36)  # 보강으로 36개월이 됐지만 실제 주문은 4개월

    result = select_forecast_method(series, n_observed=4)

    assert result.judgment["holdout_months"] == 0 and result.judgment["errors"] == {}
    assert result.judgment["forecast_method"] == candidate_methods(describe_series(series))[0]
    assert result.ambiguous
    assert result.ambiguity_reason is not None
    assert "실제 주문" in result.ambiguity_reason


def test_exactly_six_actual_months_is_enough_to_evaluate():
    result = select_forecast_method(trend_noise_series(36), n_observed=6)
    assert result.judgment["holdout_months"] == 6 and result.judgment["errors"]


# --- 되돌림: 직전 기법 제외 -----------------------------------------------------------------------


def test_excluding_the_previous_method_gives_a_different_method():
    series = seasonal_series(noise=5.0, seed=2)
    first = select_forecast_method(series).judgment["forecast_method"]

    second = select_forecast_method(series, exclude={first})

    assert second.judgment["forecast_method"] not in (None, first)
    assert second.judgment["excluded"] == [first]


def test_excluding_every_candidate_exhausts_the_choices():
    series = trend_noise_series(48)
    everything = set(candidate_methods(describe_series(series)))

    result = select_forecast_method(series, exclude=everything)

    assert result.judgment["forecast_method"] is None and result.judgment["exhausted"] is True
    assert "소진" in result.reasoning


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
def test_sample_instances_with_different_traits_get_different_methods():
    instances = load_instances()
    judgments, series = {}, {}
    for trait in ("seasonal", "sparse", "short_history"):
        row = instances[instances["trait"] == trait].iloc[0]
        collected = collect_instance_data(load_instance_inputs(row["company_id"], row["item_id"]))
        series[trait] = collected.training_series
        judgments[trait] = select_forecast_method(series[trait], n_observed=collected.n_observed_months)

    assert judgments["seasonal"].judgment["characteristics"]["kind"] == "seasonal"
    assert judgments["sparse"].judgment["characteristics"]["kind"] == "intermittent"
    assert judgments["sparse"].judgment["forecast_method"] in candidate_methods(describe_series(series["sparse"]))
    assert len({j.judgment["forecast_method"] for j in judgments.values()}) >= 2
