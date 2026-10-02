"""외부 경계 데이터 인터페이스 — `data/generated/`의 고정 샘플을 읽는다.

agent 판단 로직은 이 모듈의 함수로만 외부 데이터를 읽는다. 실제 고객사 데이터나 API로
교체할 때 이 모듈 내부만 바꾸면 되고 agent 판단 로직은 건드리지 않는다
(AGENT_NODE_LIST.md "외부 경계"). 반환 열은 공급망 표준(EDI 850 수주, 852 판매·재고)에
대응하는 이름을 쓴다. 단, 식별자는 GS1(GTIN/GLN)이 아니라 샘플 내부 식별자
(`CUST-xx`, `ITEM-nnn`)를 그대로 쓴다 — 샘플 데이터에 대응하는 실제 코드가 없다.

프로모션 여부는 `true`/`false`/`null`(기록 없음) 세 상태의 pandas `boolean`이다.
수주 생성기 내부 정책(`generator_params.json`)은 의도적으로 읽는 함수를 두지 않는다 —
현실의 공급사도 고객사의 재고 정책을 모른다.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .favorita_loader import complete_months_only

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "generated"


def _dir(data_dir: str | Path | None) -> Path:
    return Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR


def load_meta(data_dir: str | Path | None = None) -> dict:
    return json.loads((_dir(data_dir) / "sample_meta.json").read_text(encoding="utf-8"))


def load_data_end(data_dir: str | Path | None = None) -> pd.Timestamp:
    return pd.Timestamp(load_meta(data_dir)["data_end"])


def load_instances(data_dir: str | Path | None = None) -> pd.DataFrame:
    """(고객사, item) 인스턴스 목록: 상품군, 업종, 신선 여부, 계약 시작일, 합성 여부."""
    frame = pd.read_csv(_dir(data_dir) / "instances.csv", parse_dates=["contract_start"])
    return frame


def load_orders(data_dir: str | Path | None = None) -> pd.DataFrame:
    """주문 이력(EDI 850에 대응). 첫 주문 표시(`is_first_order`)와 프로모션 여부 포함."""
    return pd.read_csv(
        _dir(data_dir) / "orders.csv",
        parse_dates=["order_date"],
        dtype={"promotion": "boolean", "is_first_order": "bool", "is_synthetic": "bool"},
    )


def load_pos_daily(data_dir: str | Path | None = None) -> pd.DataFrame:
    """고객사가 공유한 일별 POS(EDI 852에 대응). `role`은 target(인스턴스 자신) 또는 similar(비슷한 제품)."""
    return pd.read_csv(
        _dir(data_dir) / "pos_daily.csv", parse_dates=["date"], dtype={"promotion": "boolean"}
    )


def load_pos_category_monthly(data_dir: str | Path | None = None) -> pd.DataFrame:
    """고객사·상품군별 월별 POS(category 범위). 완전한 달만 담겨 있다."""
    return pd.read_csv(_dir(data_dir) / "pos_category_monthly.csv", parse_dates=["month"])


def load_contracts(data_dir: str | Path | None = None) -> pd.DataFrame:
    """고객사별 계약 조건: MOQ, 결품 위약률."""
    return pd.read_csv(_dir(data_dir) / "contracts.csv")


def aggregate_monthly_orders(orders: pd.DataFrame, data_end: pd.Timestamp) -> pd.DataFrame:
    """주문을 (고객사, item, 달)별로 합산한다(완전한 달만).

    열: quantity, n_orders, has_first_order, promotion. 한 달에 프로모션 표시 주문이
    있으면 true, 모든 주문이 false로 기록돼 있으면 false, 그 외(기록 없음이 섞임)는 null.
    주문이 없는 달은 행이 없다(주문 수량 0인 달).
    """
    frame = orders.assign(month=orders["order_date"].dt.to_period("M").dt.to_timestamp())
    rows = []
    for (company, item, month), g in frame.groupby(["company_id", "item_id", "month"]):
        promo = g["promotion"]
        if promo.eq(True).any():
            status = True
        elif promo.notna().all():
            status = False
        else:
            status = None
        rows.append(
            {
                "company_id": company,
                "item_id": item,
                "month": month,
                "quantity": g["quantity"].sum(),
                "n_orders": len(g),
                "has_first_order": bool(g["is_first_order"].any()),
                "promotion": status,
            }
        )
    monthly = pd.DataFrame(rows)
    monthly["promotion"] = pd.array(monthly["promotion"].tolist(), dtype="boolean")
    return complete_months_only(monthly, data_end)


def load_similar_items(data_dir: str | Path | None = None) -> pd.DataFrame:
    """인스턴스별 비슷한 제품(같은 고객사, 같은 class) 연결: company_id, item_id, similar_item_id."""
    return pd.read_csv(_dir(data_dir) / "similar_items.csv")


@dataclass
class InstanceInputs:
    """forecast agent 한 인스턴스((회사, item))가 데이터 수집 단계에서 받는 입력 묶음.

    `market_index`는 시장 데이터(월별 지수 수준 시리즈)이며 물가 보정까지 끝난 값을 호출자가 넘긴다.
    없으면 None이다.
    """

    company_id: str
    item_id: str
    family: str
    industry: str
    perishable: bool
    data_end: pd.Timestamp
    orders: pd.DataFrame  # order_date, quantity, is_first_order, promotion
    pos_same: pd.DataFrame  # date, quantity, promotion — 같은 고객사·같은 item
    pos_similar: pd.DataFrame  # date, item_id, quantity, promotion — 비슷한 제품
    pos_category: pd.DataFrame  # month, quantity — 같은 고객사·같은 상품군(완전한 달)
    market_index: pd.Series | None = None
    refs: dict = field(default_factory=dict)


def load_instance_inputs(
    company_id: str,
    item_id: str,
    data_dir: str | Path | None = None,
    market_index: pd.Series | None = None,
) -> InstanceInputs:
    """고정 샘플에서 한 인스턴스의 입력을 읽는다. 인스턴스가 샘플에 없으면 KeyError."""
    instances = load_instances(data_dir)
    row = instances[(instances["company_id"] == company_id) & (instances["item_id"] == item_id)]
    if row.empty:
        raise KeyError(f"샘플에 없는 인스턴스: {company_id}/{item_id}")
    row = row.iloc[0]
    orders = load_orders(data_dir)
    orders = orders[(orders["company_id"] == company_id) & (orders["item_id"] == item_id)]
    pos = load_pos_daily(data_dir)
    same = pos[(pos["company_id"] == company_id) & (pos["item_id"] == item_id) & (pos["role"] == "target")]
    similar_ids = load_similar_items(data_dir)
    similar_ids = similar_ids[
        (similar_ids["company_id"] == company_id) & (similar_ids["item_id"] == item_id)
    ]["similar_item_id"]
    similar = pos[(pos["company_id"] == company_id) & pos["item_id"].isin(set(similar_ids))]
    category = load_pos_category_monthly(data_dir)
    category = category[(category["company_id"] == company_id) & (category["family"] == row["family"])]
    return InstanceInputs(
        company_id=company_id,
        item_id=item_id,
        family=row["family"],
        industry=row["industry"],
        perishable=bool(row["perishable"]),
        data_end=load_data_end(data_dir),
        orders=orders[["order_date", "quantity", "is_first_order", "promotion"]].reset_index(drop=True),
        pos_same=same[["date", "quantity", "promotion"]].reset_index(drop=True),
        pos_similar=similar[["date", "item_id", "quantity", "promotion"]].reset_index(drop=True),
        pos_category=category[["month", "quantity"]].reset_index(drop=True),
        market_index=market_index,
    )
