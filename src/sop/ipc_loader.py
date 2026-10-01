"""IPC(소비자물가지수) 로더 — 시장 데이터 물가 보정용.

사용하는 파일은 INEC가 공개한 이어붙인 시리즈(`Series IPC Empalmadas`)다. 2004년 기준
지수(2005-2014년)가 2014년 기준 지수(2015년부터)에 이미 이어붙여져 있어 이 모듈이
기준연도를 맞추지 않는다. 분류는 CCIF이며 division(2자리), group(3자리), class(4자리)
코드가 한 시트에 있다. 업종별 사용 코드는 `market_data.INDUSTRY_PRICE_INDEX` 참고.
"""

import re
from pathlib import Path

import pandas as pd

NATIONAL_SHEET = "1. NACIONAL"
_MONTHS_ES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
_HEADER_RE = re.compile(r"^([a-z]{3})-(\d{2})$")


def _parse_month(label) -> pd.Timestamp | None:
    match = _HEADER_RE.match(str(label).strip().lower())
    if not match or match.group(1) not in _MONTHS_ES:
        return None
    return pd.Timestamp(
        year=2000 + int(match.group(2)), month=_MONTHS_ES.index(match.group(1)) + 1, day=1
    )


def parse_ipc_rows(rows: list[list], codes: list[str]) -> pd.DataFrame:
    """시트 행 목록을 월 × CCIF 코드 지수 표로 바꾼다. 없는 코드는 KeyError."""
    header_idx = next(
        (i for i, r in enumerate(rows) if len(r) > 2 and r[0] == "Nivel" and str(r[1]).startswith("Cód")),
        None,
    )
    if header_idx is None:
        raise ValueError("IPC 시트에서 Nivel/Cód. CCIF 헤더 행을 찾지 못함")
    header = rows[header_idx]
    month_cols = [(j, _parse_month(h)) for j, h in enumerate(header) if _parse_month(h) is not None]
    months = [m for _, m in month_cols]

    wanted = set(codes)
    data: dict[str, list[float]] = {}
    for row in rows[header_idx + 1 :]:
        code = row[1] if len(row) > 1 else None
        if not isinstance(code, str) or code not in wanted:
            continue  # 코드가 문자열이 아닌 행(각주 등)은 건너뜀. 숫자로 읽힌 코드는 앞자리 0이 사라져 쓰지 않는다
        values = [row[j] for j, _ in month_cols]
        if any(v is None or v == "" for v in values):
            raise ValueError(f"IPC 코드 {code}에 빈 월이 있음")
        data[code] = [float(v) for v in values]
    missing = sorted(wanted - set(data))
    if missing:
        raise KeyError(f"IPC에 없는 코드: {missing}")
    frame = pd.DataFrame(data, index=pd.DatetimeIndex(months, name="month"))
    return frame[list(codes)]


def load_ipc_index(
    path: str | Path,
    codes: list[str],
    start: str | None = None,
    end: str | None = None,
    sheet: str = NATIONAL_SHEET,
) -> pd.DataFrame:
    """IPC xlsx에서 지수 시리즈를 읽는다. `start`/`end`는 'YYYY-MM' 형식(포함)."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = [list(r) for r in workbook[sheet].iter_rows(values_only=True)]
    finally:
        workbook.close()
    return parse_ipc_rows(rows, codes).loc[start:end]
