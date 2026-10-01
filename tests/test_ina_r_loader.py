from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from sop.ina_r_loader import load_ina_r_index, parse_ina_r_rows

INA_R_XLS = next(
    iter((Path(__file__).resolve().parents[1] / "data" / "raw" / "ina_r").glob("*.xls")), None
)
START = date(2013, 1, 1)


def sheet(headers, *rows):
    """INA-R 시트 모양의 행 목록: 위쪽 제목 행 + 헤더 행 + 데이터 행 + 각주 행."""
    return [
        ["", "", ""],
        ["ÍNDICE DE NIVEL DE ACTIVIDAD REGISTRADA", "", ""],
        ["Nivel", "CIIU3", "DESCRIPCIÓN CIIU3", *headers],
        *rows,
        ["", "", ""],
        ["* Datos provisionales", "", ""],
    ]


HEADERS = ["ENE.13", "FEB.13", "MAR.13", "ABR.13"]


def test_parse_keeps_zero_as_zero_and_blank_as_missing():
    """0은 그 달 매출이 실제로 0, 빈칸은 신고 자체가 없음 — 서로 다른 값으로 남는다."""
    rows = sheet(
        HEADERS,
        ["3", "D151", "CARNE", 100.0, 0.0, "", 98.5],
    )

    frame = parse_ina_r_rows(rows, ["D151"], first_month=START)

    values = frame["D151"]
    assert values.iloc[1] == 0.0 and not np.isnan(values.iloc[1])
    assert np.isnan(values.iloc[2])
    assert values.iloc[3] == 98.5
    assert frame.index[0] == pd.Timestamp("2013-01-01")


def test_parse_uses_position_for_months_despite_inconsistent_header_labels():
    headers = ["ENE.13", "FEB. 13", "MAR,13", "ABR.13*"]
    rows = sheet(headers, ["3", "D152", "LÁCTEOS", 1.0, 2.0, 3.0, 4.0])

    frame = parse_ina_r_rows(rows, ["D152"], first_month=START)

    assert frame.index.tolist() == list(pd.date_range("2013-01-01", periods=4, freq="MS"))
    assert frame.attrs["provisional_months"] == [pd.Timestamp("2013-04-01")]


def test_parse_rejects_header_that_contradicts_position():
    rows = sheet(["ENE.13", "MAR.13", "MAR.13", "ABR.13"], ["3", "D152", "LÁCTEOS", 1, 2, 3, 4])
    with pytest.raises(ValueError, match="위치로 정한 월"):
        parse_ina_r_rows(rows, ["D152"], first_month=START)


def test_parse_returns_only_requested_codes_and_raises_on_missing_code():
    rows = sheet(
        HEADERS,
        ["3", "D151", "A", 1, 2, 3, 4],
        ["3", "D153", "B", 5, 6, 7, 8],
    )
    frame = parse_ina_r_rows(rows, ["D153", "D151"], first_month=START)
    assert frame.columns.tolist() == ["D153", "D151"]
    with pytest.raises(KeyError, match="D155"):
        parse_ina_r_rows(rows, ["D155"], first_month=START)


def test_parse_rejects_duplicate_codes_and_non_numeric_cells():
    dup = sheet(HEADERS, ["3", "D151", "A", 1, 2, 3, 4], ["3", "D151", "A", 1, 2, 3, 4])
    with pytest.raises(ValueError, match="중복"):
        parse_ina_r_rows(dup, first_month=START)
    bad = sheet(HEADERS, ["3", "D151", "A", 1, "n/d", 3, 4])
    with pytest.raises(ValueError, match="숫자도 빈칸도 아님"):
        parse_ina_r_rows(bad, first_month=START)


def test_parse_does_not_compute_change_rates():
    """이 단계는 지수 시리즈까지만 만든다 — 변화율은 변화율 기준이 정해진 뒤(4단계)."""
    frame = parse_ina_r_rows(sheet(HEADERS, ["3", "D151", "A", 100, 110, 99, 120]), first_month=START)
    assert frame["D151"].tolist() == [100.0, 110.0, 99.0, 120.0]


@pytest.mark.skipif(INA_R_XLS is None, reason="INA-R 원본(data/raw/ina_r)이 없음")
def test_real_file_has_d151_to_d155_for_2013_01_to_2017_08():
    codes = ["D151", "D152", "D153", "D154", "D155"]

    frame = load_ina_r_index(INA_R_XLS, codes, start="2013-01", end="2017-08")

    assert len(frame) == 56
    assert frame.index[0] == pd.Timestamp("2013-01-01")
    assert frame.index[-1] == pd.Timestamp("2017-08-01")
    assert frame.columns.tolist() == codes
    assert frame.notna().all().all()  # 이 그룹들은 이 기간에 신고 없는 달이 없다
    assert (frame > 0).all().all()
    assert frame.attrs["provisional_months"] == []  # 잠정치는 2017년 11-12월
