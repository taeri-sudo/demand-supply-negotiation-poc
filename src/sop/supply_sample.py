"""supply_coordination이 읽는 외부 데이터의 고정 샘플 생성 (M4 2단계).

수주(납기·납품한 양이 붙은 주문)는 수주 생성기(`order_generator.py`)가 만들고, 이 모듈은 그 위에 얹는 나머지 입력을 만든다:
사람이 정한 수량, 계약 조건(최소 구매 약정·공급 보장 물량·계약 단위), 재고·생산 중 물량, 라인 기준 정보(`line_assignments`).
`sample_builder.build_sample`이 호출하며 같은 입력이면 같은 파일이 나온다. 재고·생산 중 물량은 물리적 실행(외부 경계)의 결과를
대신하는 고정 스텁이고(AGENT_NODE_LIST.md "외부 경계"), 약정·보장 물량은 샘플 주문량에서 정한 값이다.
"""

from datetime import date

import pandas as pd

from .capacity import normal_monthly_demand

# 라인 기준 정보: 상품군 → 주 라인. FROZEN FOODS는 인스턴스 하나만 쓰는 라인이다.
FAMILY_PRIMARY_LINE = {
    "BEVERAGES": "LINE-BEV-1",
    "DAIRY": "LINE-FOOD-1",
    "BREAD/BAKERY": "LINE-FOOD-1",
    "MEATS": "LINE-FOOD-1",
    "POULTRY": "LINE-FOOD-1",
    "GROCERY I": "LINE-FOOD-2",
    "FROZEN FOODS": "LINE-FROZEN-1",
    "DELI": "LINE-FOOD-2",
    "LIQUOR,WINE,BEER": "LINE-LIQ-1",
    "PREPARED FOODS": "LINE-FOOD-2",
}
FAMILY_ALTERNATE_LINE = {"GROCERY I": "LINE-FOOD-1"}  # M4는 주 라인만 쓴다. 대체 라인은 M5에서 쓴다
LINE_VALID_FROM = date(2013, 1, 1)

DEFAULT_CONTRACT_UNIT = 6.0  # 상품별 기본 계약 단위. 고객사별 덮어쓰기는 그 고객사의 MOQ다

NEW_PRODUCT_ITEM = "ITEM-NEW-001"  # 주문 이력이 없는 신제품(사람이 정한 수량으로만 들어온다)
NEW_PRODUCT_FAMILY = "DAIRY"
NEW_CUSTOMER_ID = "CUST-NEW-01"  # 계약은 맺었지만 아직 주문하지 않은 새 고객사
NEW_CUSTOMER_ITEM_FAMILY = "BEVERAGES"

COMMITMENT_PERIOD_START = date(2017, 1, 1)
COMMITMENT_PERIOD_END = date(2017, 12, 31)
COMMITMENT_EVERY = 2  # 인스턴스 둘 중 하나에 최소 구매 약정을 둔다. 구속력 있음과 없음이 번갈아 나온다
COMMITMENT_TOTAL_SHARE = 0.9  # 약정 총량 = 평소 월 수요 x 기간 개월 수 x 이 비율
GUARANTEE_EVERY = 5  # 인스턴스 다섯 중 하나에 공급 보장 물량을 둔다
GUARANTEE_SHARE = 0.3  # 공급 보장 물량 = 평소 월 수요 x 이 비율
INVENTORY_SHARE = 0.1  # 재고 스텁 = 평소 월 수요 합계 x 이 비율
WIP_ORDERS = 2  # 생산 중 물량 스텁을 묶는 앞으로 납기가 올 수주 수


def round_down_to_unit(quantity: float, unit: float) -> float:
    return float(int(round(quantity / unit, 9)) * unit)


def build_supply_inputs(
    orders: pd.DataFrame, instances: pd.DataFrame, moq_by_company: dict[str, float], data_end: pd.Timestamp
) -> dict[str, pd.DataFrame]:
    """파일 이름 → 내용. `orders`는 납품한 양이 붙은 전체 주문, `instances`는 (고객사, item) 인스턴스 목록이다."""
    demand = normal_monthly_demand(orders, data_end)
    pairs = sorted(zip(instances["company_id"], instances["item_id"]))
    family_of = dict(zip(instances["item_id"], instances["family"]))
    entered_at = data_end.date().isoformat()

    def unit(company: str, item: str) -> float:
        return moq_by_company.get(company, DEFAULT_CONTRACT_UNIT)

    # 라인 기준 정보
    items = sorted(set(family_of) | {NEW_PRODUCT_ITEM})
    family_of = {**family_of, NEW_PRODUCT_ITEM: NEW_PRODUCT_FAMILY}
    assignments = []
    for item in items:
        family = family_of[item]
        assignments.append(
            {"item_id": item, "line_id": FAMILY_PRIMARY_LINE[family], "preference": "primary", "valid_from": LINE_VALID_FROM.isoformat()}
        )
        if family in FAMILY_ALTERNATE_LINE:
            assignments.append(
                {"item_id": item, "line_id": FAMILY_ALTERNATE_LINE[family], "preference": "alternate", "valid_from": LINE_VALID_FROM.isoformat()}
            )

    # 계약 단위: 상품별 기본값(item_id만) + 고객사별 덮어쓰기(company_id만)
    units = [{"item_id": item, "company_id": None, "unit": DEFAULT_CONTRACT_UNIT} for item in items]
    units += [{"item_id": None, "company_id": c, "unit": m} for c, m in sorted(moq_by_company.items())]

    # 최소 구매 약정과 공급 보장 물량
    commitments, guarantees = [], []
    months_in_period = 12
    for k, (company, item) in enumerate(pairs):
        monthly = demand.get((company, item), 0.0)
        if monthly <= 0:  # 평소 주문이 없는 인스턴스에는 약정도 보장 물량도 두지 않는다
            continue
        if k % COMMITMENT_EVERY == 0:
            contract_start = instances[(instances["company_id"] == company) & (instances["item_id"] == item)]["contract_start"].iloc[0]
            start = max(COMMITMENT_PERIOD_START, pd.Timestamp(contract_start).date())
            commitments.append(
                {
                    "company_id": company, "item_id": item, "binding": (k // COMMITMENT_EVERY) % 2 == 0,
                    "total_quantity": round_down_to_unit(monthly * months_in_period * COMMITMENT_TOTAL_SHARE, unit(company, item)),
                    "period_start": start.isoformat(), "period_end": COMMITMENT_PERIOD_END.isoformat(),
                }
            )
        if k % GUARANTEE_EVERY == 1:
            guarantees.append(
                {"company_id": company, "item_id": item, "quantity": round_down_to_unit(monthly * GUARANTEE_SHARE, unit(company, item))}
            )

    # 사람이 정한 수량: 신제품 첫 물량(고객사 없음), 새 고객사의 예상 물량, 프로모션 이력이 없는 고객사·item의 프로모션 수량
    promo_company, promo_item = next((c, i) for c, i in pairs if c == "CUST-03")
    new_customer_item = next(i for i in items if family_of[i] == NEW_CUSTOMER_ITEM_FAMILY)
    manual = [
        {"demand_id": "MAN-0001", "source": "manual", "input_reason": "new_product", "company_id": None,
         "item_id": NEW_PRODUCT_ITEM, "quantity": 20 * DEFAULT_CONTRACT_UNIT, "entered_at": entered_at},
        {"demand_id": "MAN-0002", "source": "manual", "input_reason": "new_customer", "company_id": NEW_CUSTOMER_ID,
         "item_id": new_customer_item, "quantity": 50 * DEFAULT_CONTRACT_UNIT, "entered_at": entered_at},
        {"demand_id": "MAN-0003", "source": "manual", "input_reason": "promotion", "company_id": promo_company,
         "item_id": promo_item, "quantity": round_down_to_unit(0.3 * demand.get((promo_company, promo_item), 0.0), unit(promo_company, promo_item)),
         "entered_at": entered_at},
    ]

    # 재고와 생산 중 물량(고정 스텁). 생산 중 물량은 앞으로 납기가 올 수주 일부에 묶인다.
    totals = demand.groupby("item_id").sum()
    inventory = [
        {"item_id": item, "quantity": round_down_to_unit(INVENTORY_SHARE * float(totals.get(item, 0.0)), DEFAULT_CONTRACT_UNIT)}
        for item in items
    ]
    open_orders = orders[orders["due_date"] > data_end].sort_values("demand_id")
    wip = []
    for row in open_orders.drop_duplicates("item_id").head(WIP_ORDERS).itertuples():
        wip.append(
            {"item_id": row.item_id, "quantity": round_down_to_unit(0.5 * float(row.quantity), unit(str(row.company_id), str(row.item_id))),  # pyright: ignore[reportArgumentType]
             "demand_ref": row.demand_id}
        )

    return {
        "line_assignments.csv": pd.DataFrame(assignments),
        "contract_units.csv": pd.DataFrame(units),
        "min_commitments.csv": pd.DataFrame(commitments),
        "supply_guarantees.csv": pd.DataFrame(guarantees),
        "manual_quantities.csv": pd.DataFrame(manual),
        "inventory.csv": pd.DataFrame(inventory),
        "work_in_progress.csv": pd.DataFrame(wip),
    }
