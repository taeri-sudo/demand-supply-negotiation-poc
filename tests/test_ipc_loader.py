from pathlib import Path

import openpyxl
import pandas as pd
import pytest

from sop.ipc_loader import NATIONAL_SHEET, load_ipc_index, parse_ipc_rows

IPC_XLSX = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "raw"
    / "ipc"
    / "Series IPC Empalmadas"
    / "ipc_ind_nac_reg_ciud_emp_clase_06_2026.xlsx"
)

HEADER = ["Nivel", "Cód. CCIF", "Descripción CCIF", "ene-14", "feb-14", "mar-14"]


def rows(*data):
    return [[None] * 6, ["ÍNDICE DE PRECIOS AL CONSUMIDOR - IPC -"], HEADER, *data, ["Nota: "]]


def test_parse_reads_codes_as_strings_keeping_leading_zero():
    frame = parse_ipc_rows(
        rows(
            ["Grupo", "011", "Alimentos", 100.0, 101.0, 102.5],
            ["Grupo", "012", "Bebidas no alcohólicas", 100.0, 100.5, 101.0],
        ),
        ["012", "011"],
    )

    assert frame.columns.tolist() == ["012", "011"]
    assert frame.index.tolist() == list(pd.date_range("2014-01-01", periods=3, freq="MS"))
    assert frame["011"].tolist() == [100.0, 101.0, 102.5]


def test_parse_raises_on_missing_code_and_blank_month():
    with pytest.raises(KeyError, match="021"):
        parse_ipc_rows(rows(["Grupo", "011", "Alimentos", 1, 2, 3]), ["021"])
    with pytest.raises(ValueError, match="빈 월"):
        parse_ipc_rows(rows(["Grupo", "011", "Alimentos", 1, None, 3]), ["011"])


def test_load_ipc_index_reads_xlsx_and_slices_months(tmp_path):
    path = tmp_path / "ipc.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = NATIONAL_SHEET
    for row in rows(["Grupo", "011", "Alimentos", 100.0, 101.0, 102.0]):
        sheet.append(row)
    workbook.save(path)

    frame = load_ipc_index(path, ["011"], start="2014-02", end="2014-03")

    assert frame["011"].tolist() == [101.0, 102.0]


@pytest.mark.skipif(not IPC_XLSX.exists(), reason="IPC 원본(data/raw/ipc)이 없음")
def test_real_file_covers_2013_01_to_2017_08_for_the_three_industry_groups():
    frame = load_ipc_index(IPC_XLSX, ["011", "012", "021"], start="2013-01", end="2017-08")

    assert len(frame) == 56
    assert frame.index[0] == pd.Timestamp("2013-01-01")
    assert frame.index[-1] == pd.Timestamp("2017-08-01")
    assert frame.notna().all().all()
    # 2014년 기준(2014=100)으로 이어붙인 시리즈라 2014년 평균이 100 근처다
    assert frame.loc["2014", "011"].mean() == pytest.approx(100.0, abs=2.0)
