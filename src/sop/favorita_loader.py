"""Favorita 로더 — 외부 경계의 POS 데이터 인터페이스.

원본 파일은 수정하지 않고, 이 모듈 안에서만 매장(store_nbr)을 고객사(company_id)로,
상품(item_nbr)을 item_id로 간주해 매핑한다(AGENT_NODE_LIST.md "외부 경계" 데이터
대체 가정). 정제는 하지 않는다 — 음수 판매량(반품)과 결측은 그대로 두고, 오염 판단과
정제는 forecast agent의 데이터 소스 판단(M2 3단계)이 맡는다.

프로모션 여부는 `true`/`false`/`null`(기록 없음) 세 상태를 pandas `boolean`으로
유지한다. Favorita는 2014년 4월부터만 기록돼 있어 그 이전 값은 `null`이며, 이를
`false`로 채우지 않는다.

월별 합산은 완전한 달만 쓴다(데이터가 중간에 끝나는 마지막 달 제외). Favorita 원본은
행이 있는 날만 담고 판매가 없는 날은 행이 없으므로, 월 합계에서 행이 없는 날은 판매량 0으로 취급된다.
"""

from pathlib import Path

import pandas as pd

from .favorita_mapping import FAMILY_MAPPING

_POS_DTYPES = {
    "store_nbr": "int16",
    "item_nbr": "int32",
    "unit_sales": "float32",
    "onpromotion": "boolean",
}


def store_to_company_id(store_nbr: int) -> str:
    return f"CUST-{int(store_nbr):02d}"


def item_to_item_id(item_nbr: int) -> str:
    return f"ITEM-{int(item_nbr)}"


def load_items(path: str | Path) -> pd.DataFrame:
    """items.csv를 읽어 상품군 매핑을 붙인다.

    반환 열: item_nbr, item_id, family, class_id, perishable(bool), industry(범위 밖이면
    None), in_scope(bool). items.csv에 매핑 표에 없는 상품군이 있으면 오류 — 표가
    33개 상품군 전부를 덮고 있다는 전제가 깨졌다는 뜻이다.
    """
    items = pd.read_csv(path)
    unmapped = sorted(set(items["family"]) - set(FAMILY_MAPPING))
    if unmapped:
        raise ValueError(f"FAMILY_MAPPING에 없는 상품군: {unmapped}")
    items = items.rename(columns={"class": "class_id"})
    items["item_id"] = items["item_nbr"].map(item_to_item_id)
    items["perishable"] = items["perishable"].astype(bool)
    items["industry"] = items["family"].map(lambda f: FAMILY_MAPPING[f].industry)
    items["in_scope"] = items["family"].map(lambda f: FAMILY_MAPPING[f].in_scope)
    return items[["item_nbr", "item_id", "family", "class_id", "perishable", "industry", "in_scope"]]


def scan_pos(
    train_path: str | Path,
    store_nbrs: set[int],
    item_nbrs: set[int],
    chunksize: int = 5_000_000,
) -> pd.DataFrame:
    """train.csv를 청크로 읽어 지정한 매장·상품의 일별 행만 모은다.

    반환 열: date, store_nbr, item_nbr, quantity, promotion. `promotion`은
    true/false/null 세 상태의 boolean이다.
    """
    kept = []
    reader = pd.read_csv(
        train_path,
        usecols=["date", "store_nbr", "item_nbr", "unit_sales", "onpromotion"],
        dtype=_POS_DTYPES,
        parse_dates=["date"],
        chunksize=chunksize,
    )
    for chunk in reader:
        mask = chunk["store_nbr"].isin(store_nbrs) & chunk["item_nbr"].isin(item_nbrs)
        if mask.any():
            kept.append(chunk[mask])
    daily = pd.concat(kept, ignore_index=True)
    return daily.rename(columns={"unit_sales": "quantity", "onpromotion": "promotion"})


def to_boundary_format(daily: pd.DataFrame) -> pd.DataFrame:
    """store_nbr/item_nbr 열을 고객사·item 식별자(company_id, item_id)로 바꾼다."""
    out = daily.copy()
    out.insert(1, "company_id", out["store_nbr"].map(store_to_company_id))
    out.insert(2, "item_id", out["item_nbr"].map(item_to_item_id))
    return out.drop(columns=["store_nbr", "item_nbr"])


def complete_months_only(monthly: pd.DataFrame, data_end: pd.Timestamp) -> pd.DataFrame:
    """달의 마지막 날이 데이터 끝 날짜 이후인(= 데이터가 중간에 끝나는) 달을 제외한다."""
    month_end = monthly["month"] + pd.offsets.MonthEnd(0)
    return monthly[month_end <= pd.Timestamp(data_end).normalize()].reset_index(drop=True)


def monthly_promotion_status(monthly: pd.DataFrame) -> pd.Series:
    """한 (고객사, item)의 월별 프로모션 여부 — true/false/null 세 상태.

    그 달에 프로모션 일이 하나라도 있으면 true. 모든 판매일이 기록돼 있고 프로모션이
    없을 때만 false. 기록 없는 날이 섞여 있으면 "없음"이라고 단정할 수 없어 null이다.
    """
    values = []
    for promo, nonpromo, unrecorded in zip(
        monthly["promo_days"], monthly["nonpromo_days"], monthly["unrecorded_days"]
    ):
        if promo > 0:
            values.append(True)
        elif unrecorded == 0 and nonpromo > 0:
            values.append(False)
        else:
            values.append(None)
    return pd.Series(pd.array(values, dtype="boolean"), index=monthly.index)


def aggregate_monthly(
    daily: pd.DataFrame,
    group_cols: list[str],
    data_end: pd.Timestamp | None = None,
    with_promotion_status: bool = False,
) -> pd.DataFrame:
    """일별 행을 group_cols + 월 단위로 합산한다(완전한 달만).

    입력 열: date, quantity, promotion과 group_cols. 출력 열: group_cols, month(달의
    첫날), quantity, sales_days(행이 있는 날 수), promo_days/nonpromo_days/
    unrecorded_days(프로모션 true/false/null인 행 수). `data_end`가 None이면 입력의
    마지막 날짜를 데이터 끝으로 본다. 입력이 일부 매장·상품만 담고 있어 마지막 날짜가
    실제 데이터 끝과 다를 수 있으면 `data_end`를 직접 넘긴다.
    """
    frame = daily.assign(month=daily["date"].dt.to_period("M").dt.to_timestamp())
    promotion = frame["promotion"]
    frame = frame.assign(
        _promo=promotion.eq(True).fillna(False).astype(int),
        _nonpromo=promotion.eq(False).fillna(False).astype(int),
        _unrecorded=promotion.isna().astype(int),
        _days=1,
    )
    grouped = frame.groupby([*group_cols, "month"], as_index=False).agg(
        quantity=("quantity", "sum"),
        sales_days=("_days", "sum"),
        promo_days=("_promo", "sum"),
        nonpromo_days=("_nonpromo", "sum"),
        unrecorded_days=("_unrecorded", "sum"),
    )
    end = data_end if data_end is not None else daily["date"].max()
    result = complete_months_only(grouped, end)
    if with_promotion_status:
        result["promotion"] = monthly_promotion_status(result)
    return result
