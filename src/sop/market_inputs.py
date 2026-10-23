"""인스턴스의 시장 데이터 입력을 원본 파일에서 만든다 (AGENT_NODE_LIST.md forecast agent "입력"의 시장 데이터 처리).

상품군에 대응하는 INA-R 그룹(`favorita_mapping`)의 명목 지수를 읽어 업종의 IPC 그룹 지수로 마지막 완전한 달의 물가 수준에 맞춰 보정한다
(`market_data.deflate_market_index`). 우리 회사 매출은 INA-R에 포함돼 있지 않아 차감하지 않는다(`INA_R_SOURCE`). 원본 파일은 저장소에 없고
`data/raw/`에서 읽는다(DESIGN.md "데이터 출처"). `InstanceInputs.market_group_index`는 그룹별 열, `market_index`는 그 열들의 단순 평균이다.
"""

from pathlib import Path

import pandas as pd

from .data_source_judgment import last_complete_month
from .favorita_mapping import FAMILY_MAPPING
from .ina_r_loader import load_ina_r_index
from .ipc_loader import load_ipc_index
from .market_data import INDUSTRY_PRICE_INDEX, INA_R_SOURCE, deflate_market_index

RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"
IPC_XLSX = RAW_DIR / "ipc" / "Series IPC Empalmadas" / "ipc_ind_nac_reg_ciud_emp_clase_06_2026.xlsx"
MARKET_START = "2013-01"


def ina_r_path(raw_dir: Path = RAW_DIR) -> Path:
    path = next(iter((raw_dir / "ina_r").glob("*.xls")), None)
    if path is None:
        raise FileNotFoundError(f"INA-R 원본이 없음: {raw_dir / 'ina_r'}")
    return path


def load_market_inputs(
    family: str, data_end: pd.Timestamp, raw_dir: Path = RAW_DIR
) -> tuple[pd.Series, pd.DataFrame] | None:
    """상품군의 시장 입력 `(market_index, market_group_index)`. 시장 흐름이 없는 상품군(범위 밖)이면 None이다."""
    mapping = FAMILY_MAPPING[family]
    if not mapping.in_scope or mapping.industry is None:
        return None
    assert not INA_R_SOURCE.includes_own_sales  # 우리 매출을 차감하지 않는다
    base = last_complete_month(data_end)
    end = f"{base:%Y-%m}"
    nominal = load_ina_r_index(ina_r_path(raw_dir), list(mapping.market_groups), start=MARKET_START, end=end)
    ipc_path = raw_dir / "ipc" / IPC_XLSX.relative_to(RAW_DIR / "ipc")
    price = load_ipc_index(ipc_path, [INDUSTRY_PRICE_INDEX[mapping.industry]], start=MARKET_START, end=end).iloc[:, 0]
    groups = pd.DataFrame({code: deflate_market_index(nominal[code], price, base) for code in nominal.columns})
    return groups.mean(axis=1).rename("market"), groups
