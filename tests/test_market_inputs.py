"""인스턴스의 시장 데이터 입력 (`market_inputs.py`)과 `sample_instance_runtimes(with_market=True)` 경로.

[테스트 전용 입력] `data/raw`의 INA-R·IPC 원본과 `data/generated` 샘플을 읽는다. 파일이 없으면 건너뛴다.
"""

import io
from contextlib import redirect_stdout

import pandas as pd
import pytest

from sop import cycle_runner
from sop.cycle_runner import sample_instance_runtimes
from sop.forecast_steps import run_forecast_steps
from sop.ina_r_loader import load_ina_r_index
from sop.market_inputs import IPC_XLSX, RAW_DIR, ina_r_path, load_market_inputs

needs_raw = pytest.mark.skipif(
    not IPC_XLSX.exists() or not list((RAW_DIR / "ina_r").glob("*.xls")), reason="data/raw의 INA-R·IPC 원본이 없음 — DESIGN.md 데이터 출처 참고"
)
needs_sample = pytest.mark.skipif(
    not (RAW_DIR.parent / "generated" / "sample_meta.json").exists(), reason="data/generated 샘플이 없음 — `python -m sop.sample_builder`로 만든다"
)
DATA_END = pd.Timestamp("2017-08-15")


@needs_raw
def test_a_single_group_family_gets_its_group_deflated_to_the_last_complete_months_price_level():
    loaded = load_market_inputs("LIQUOR,WINE,BEER", DATA_END)
    assert loaded is not None
    market, groups = loaded

    assert list(groups.columns) == ["D155"] and groups.index.min() == pd.Timestamp("2013-01-01") and groups.index.max() == pd.Timestamp("2017-07-01")
    nominal = load_ina_r_index(ina_r_path(), ["D155"], start="2013-01", end="2017-07")["D155"]
    assert groups["D155"].loc["2017-07-01"] == pytest.approx(nominal.loc["2017-07-01"])  # 기준월은 물가가 1이라 그대로다
    assert groups["D155"].loc["2013-01-01"] != pytest.approx(nominal.loc["2013-01-01"])  # 다른 달은 물가 보정으로 바뀐다
    assert market.equals(groups["D155"].rename("market"))  # 그룹이 하나면 평균이 그 그룹이다


@needs_raw
def test_a_whole_food_processing_family_gets_the_four_groups_and_their_simple_average():
    loaded = load_market_inputs("DELI", DATA_END)
    assert loaded is not None
    market, groups = loaded

    assert list(groups.columns) == ["D151", "D152", "D153", "D154"]
    assert market.equals(groups.mean(axis=1).rename("market"))
    assert not groups.isna().any().any()


@needs_raw
def test_a_family_outside_the_scope_has_no_market_input():
    assert load_market_inputs("PRODUCE", DATA_END) is None


@needs_raw
@needs_sample
def test_the_sample_runtimes_carry_market_data_by_default_and_read_each_family_once(monkeypatch):
    pairs = (("CUST-03", "ITEM-1047679"), ("CUST-44", "ITEM-1047679"), ("CUST-44", "ITEM-1080001"))
    calls = []
    original = cycle_runner.load_market_inputs
    monkeypatch.setattr(cycle_runner, "load_market_inputs", lambda family, data_end, raw_dir: calls.append(family) or original(family, data_end, raw_dir))

    with_market = sample_instance_runtimes(pairs)
    without = sample_instance_runtimes(pairs, with_market=False)

    assert calls == ["BEVERAGES", "LIQUOR,WINE,BEER"]  # 같은 상품군은 한 번만 읽는다
    assert all(rt.inputs.market_group_index is not None and rt.inputs.market_index is not None for rt in with_market.values())
    assert all(rt.inputs.market_group_index is None and rt.inputs.market_index is None for rt in without.values())
    first, second = with_market[pairs[0]].inputs.market_group_index, with_market[pairs[1]].inputs.market_group_index
    assert first is not None and second is not None and first.equals(second)  # 같은 상품군은 같은 시장 입력이다


@needs_raw
@needs_sample
def test_the_sample_instances_split_into_significant_not_significant_and_no_evidence_trends():
    def trend_of(company: str, item: str):
        with redirect_stdout(io.StringIO()):
            result = run_forecast_steps(sample_instance_runtimes([(company, item)])[company, item].inputs)
        kept = any(d.driver == "category_trend" for a in result.assumptions for d in a.drivers)
        return kept, [reason for d in result.excluded_drivers if d.driver == "category_trend" for reason in d.reasons]

    assert trend_of("CUST-44", "ITEM-1047679") == (True, [])  # 유의함: category_trend 가정이 남는다
    assert trend_of("CUST-03", "ITEM-1047679") == (False, ["no_significant_effect"])  # 신뢰구간이 0을 포함한다
    assert trend_of("CUST-03", "ITEM-946671") == (False, ["no_evidence"])  # 시장이 아니라 우리 POS의 변화율을 만들 수 없다
