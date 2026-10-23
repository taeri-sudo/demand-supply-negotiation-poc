"""외부 경계 데이터 인터페이스 — `data/generated/`의 고정 샘플을 읽는다.

agent 판단 로직은 이 모듈의 함수로만 외부 데이터를 읽는다. 실제 고객사 데이터나 API로
교체할 때 이 모듈 내부만 바꾸면 되고 agent 판단 로직은 건드리지 않는다
(AGENT_NODE_LIST.md "외부 경계"). 반환 열은 공급망 표준(EDI 850 수주, 852 판매·재고)에
대응하는 이름을 쓴다. 단, 식별자는 GS1(GTIN/GLN)이 아니라 샘플 내부 식별자
(`CUST-xx`, `ITEM-nnn`)를 그대로 쓴다 — 샘플 데이터에 대응하는 실제 코드가 없다.

supply_coordination이 읽는 입력(수주, 사람이 정한 수량, 계약 조건, 재고·생산 중 물량, 라인 기준 정보)도 이 모듈의 함수로 읽는다.
수주(`load_sales_orders`)는 forecast의 주문 이력(`load_orders`)과 같은 파일이고, 납기와 납품한 양이 붙어 있다. 납품이 끝난 수주는
forecast의 주문 이력이 되고, 수주잔고(수주 - 납품한 양)와 미납분(수주잔고 중 납기가 지난 것)은 이 데이터에서 계산한다.

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


def load_sales_orders(data_dir: str | Path | None = None) -> pd.DataFrame:
    """수주(EDI 850에 대응): demand_id, company_id, item_id, quantity, order_date, due_date, delivered_quantity.

    `due_date`는 고객사 도착 기준이다. 스냅샷 기준일 뒤에 납기가 오는 수주(앞으로 납기가 올 수주)와 납기가 지났지만 일부만 납품된
    수주(미납분)가 포함된다.
    """
    frame = pd.read_csv(_dir(data_dir) / "orders.csv", parse_dates=["order_date", "due_date"])
    return frame[
        ["demand_id", "company_id", "item_id", "quantity", "order_date", "due_date", "delivered_quantity"]
    ]


def load_manual_quantities(data_dir: str | Path | None = None) -> pd.DataFrame:
    """사람이 정한 수량: demand_id, source("manual"), input_reason(new_customer/new_product/promotion), company_id(없을 수 있음),
    item_id, quantity, entered_at. 주문이 없는 수량이다."""
    return pd.read_csv(_dir(data_dir) / "manual_quantities.csv", parse_dates=["entered_at"])


def load_min_commitments(data_dir: str | Path | None = None) -> pd.DataFrame:
    """최소 구매 약정: company_id, item_id, binding(구속력 있음), total_quantity, period_start, period_end."""
    return pd.read_csv(
        _dir(data_dir) / "min_commitments.csv", parse_dates=["period_start", "period_end"], dtype={"binding": "bool"}
    )


def load_supply_guarantees(data_dir: str | Path | None = None) -> pd.DataFrame:
    """공급 보장 물량: company_id, item_id, quantity(계획 주기당)."""
    return pd.read_csv(_dir(data_dir) / "supply_guarantees.csv")


def load_contract_units(data_dir: str | Path | None = None) -> pd.DataFrame:
    """계약 단위: 상품별 기본값(item_id만 있는 행)과 고객사별 덮어쓰기(company_id만 있는 행). 값을 찾을 때는 `contract_unit`을 쓴다."""
    return pd.read_csv(_dir(data_dir) / "contract_units.csv")


def contract_unit(units: pd.DataFrame, company_id: str | None, item_id: str) -> float:
    """수량이 따라야 하는 계약 단위. 고객사별 덮어쓰기가 있으면 그 값, 없으면 상품별 기본값이다. 둘 다 없으면 KeyError."""
    if company_id is not None:
        override = units[(units["company_id"] == company_id) & units["item_id"].isna()]
        if not override.empty:
            return float(override["unit"].iloc[0])
    default = units[(units["item_id"] == item_id) & units["company_id"].isna()]
    if default.empty:
        raise KeyError(f"계약 단위가 없음: {company_id}/{item_id}")
    return float(default["unit"].iloc[0])


def load_inventory(data_dir: str | Path | None = None) -> pd.DataFrame:
    """item별 재고: item_id, quantity. 물리적 실행(외부 경계)의 결과를 대신하는 고정 스텁이다."""
    return pd.read_csv(_dir(data_dir) / "inventory.csv")


def load_work_in_progress(data_dir: str | Path | None = None) -> pd.DataFrame:
    """생산 중 물량(계획된 생산량 - 완료된 양): item_id, quantity, demand_ref(그 물량이 묶인 수주의 demand_id). 고정 스텁이다."""
    return pd.read_csv(_dir(data_dir) / "work_in_progress.csv")


def load_line_assignments(data_dir: str | Path | None = None) -> pd.DataFrame:
    """라인 기준 정보: item_id, line_id, preference(primary/alternate), valid_from. 어느 item을 어느 라인에서 만들 수 있는가(N:M)."""
    return pd.read_csv(_dir(data_dir) / "line_assignments.csv", parse_dates=["valid_from"])


def primary_line(assignments: pd.DataFrame, item_id: str, on: pd.Timestamp) -> str:
    """`on` 시점에 item의 주 라인(`preference: "primary"`, 적용 시작일이 `on` 이전인 것 중 가장 늦은 것). 없으면 KeyError. M4는 주 라인만 쓴다."""
    rows = assignments[
        (assignments["item_id"] == item_id) & (assignments["preference"] == "primary") & (assignments["valid_from"] <= on)
    ]
    if rows.empty:
        raise KeyError(f"{on.date()} 시점에 주 라인이 없는 item: {item_id}")
    return str(rows.sort_values("valid_from")["line_id"].iloc[-1])


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
    # 변화율 계산용: 상품군에 대응하는 INA-R 그룹별 월별 지수(물가 보정 완료, 열 = D코드). 없으면
    # `market_index`를 그룹 하나로 본다.
    market_group_index: pd.DataFrame | None = None
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
