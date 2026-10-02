"""INA-R(에콰도르 통계청 등록 활동 수준 지수) 로더 — 시장 데이터 인터페이스.

원본은 CIIU Rev.3 3자리 그룹별 월별 **지수**(2002=100)이며 매출액이 아니다.
이 모듈은 지수 시리즈를 그대로 읽기만 하고 변화율은 만들지 않는다(변화율 기준은
M2 4단계 전에 정해진다 — DESIGN.md "아직 결정 안 된 것").

값 해석(원본 각주): 0은 그 달 매출이 실제로 0이라는 뜻이고, 빈칸은 신고 자체가 없다는
뜻이다. 빈칸은 0으로 채우지 않고 결측(NaN)으로 유지한다.

월 열은 헤더 문자열이 아니라 위치로 정한다. 헤더 표기가 일정하지 않기 때문이다
(`ENE.13`, `JUN,13`, `FEB. 14`, 잠정치 `NOV.17*` 등). 대신 헤더가 월로 읽히면 위치로
정한 월과 일치하는지 확인한다.
"""

import re
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

INDEX_SHEET = "Indices"
FIRST_MONTH = date(2003, 1, 1)  # 시리즈의 첫 열은 2003년 1월
_MONTHS_ES = ["ENE", "FEB", "MAR", "ABR", "MAY", "JUN", "JUL", "AGO", "SEP", "OCT", "NOV", "DIC"]
_HEADER_RE = re.compile(r"^([A-Z]{3})[.,]?\s*(\d{2})\*?$")


def _month_start(offset: int, first_month: date) -> pd.Timestamp:
    return pd.Timestamp(first_month) + pd.DateOffset(months=offset)


def _check_header(header: str, expected: pd.Timestamp) -> None:
    match = _HEADER_RE.match(header.strip().upper())
    if not match:
        return  # 월로 읽히지 않는 표기는 위치를 믿는다
    token, year2 = match.groups()
    if token not in _MONTHS_ES:
        return
    if _MONTHS_ES.index(token) + 1 != expected.month or int(year2) != expected.year % 100:
        raise ValueError(f"INA-R 헤더 {header!r}가 위치로 정한 월 {expected:%Y-%m}과 다름")


def _to_value(cell) -> float:
    if isinstance(cell, str):
        if cell.strip() == "":
            return float("nan")
        raise ValueError(f"INA-R 값 셀이 숫자도 빈칸도 아님: {cell!r}")
    return float(cell)


def parse_ina_r_rows(
    rows: Sequence[Sequence[Any]], codes: list[str] | None = None, first_month: date = FIRST_MONTH
) -> pd.DataFrame:
    """시트 행 목록(헤더 행 포함)을 월 × 코드 지수 표로 바꾼다.

    `codes`가 None이면 전체 코드를, 아니면 지정한 코드만 반환한다(없는 코드는
    KeyError). 열 이름은 CIIU3 코드, 인덱스는 달의 첫날이다. 잠정치로 표시된 월은
    `attrs["provisional_months"]`에 담는다.
    """
    header_idx = next((i for i, r in enumerate(rows) if len(r) > 1 and r[1] == "CIIU3"), None)
    if header_idx is None:
        raise ValueError("INA-R 시트에서 CIIU3 헤더 행을 찾지 못함")
    header = rows[header_idx]
    n_months = len(header) - 3
    months = [_month_start(i, first_month) for i in range(n_months)]
    provisional = []
    for i, label in enumerate(header[3:]):
        text = str(label)
        _check_header(text, months[i])
        if text.strip().endswith("*"):
            provisional.append(months[i])

    wanted = set(codes) if codes is not None else None
    data: dict[str, list[float]] = {}
    for row in rows[header_idx + 1 :]:
        code = str(row[1]).strip() if len(row) > 1 else ""
        if not code or len(row) < 3 + n_months:
            continue  # 각주 행 등
        if wanted is not None and code not in wanted:
            continue
        if code in data:
            raise ValueError(f"INA-R 코드 중복: {code}")
        data[code] = [_to_value(c) for c in row[3 : 3 + n_months]]
    if wanted is not None:
        missing = sorted(wanted - set(data))
        if missing:
            raise KeyError(f"INA-R에 없는 코드: {missing}")
    frame = pd.DataFrame(data, index=pd.DatetimeIndex(months, name="month"))
    if codes is not None:
        frame = frame[list(codes)]
    frame.attrs["provisional_months"] = provisional
    return frame


def load_ina_r_index(
    path: str | Path,
    codes: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """INA-R xls 파일에서 지수 시리즈를 읽는다. `start`/`end`는 'YYYY-MM' 형식(포함)."""
    import xlrd

    workbook = xlrd.open_workbook(str(path))
    sheet = workbook.sheet_by_name(INDEX_SHEET)
    rows = [sheet.row_values(i) for i in range(sheet.nrows)]
    frame = parse_ina_r_rows(rows, codes)
    provisional = frame.attrs["provisional_months"]
    frame = frame.loc[start:end]
    frame.attrs["provisional_months"] = [m for m in provisional if m in frame.index]
    return frame
