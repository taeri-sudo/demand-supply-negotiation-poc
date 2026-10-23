"""외부 데이터 제공자 테스트용 도구: 작은 고정 샘플 파일 묶음을 임시 폴더에 쓴다.

[테스트 전용 입력] 라인·item·수주·계약 값은 fixture 값이다(`data/generated`의 실제 샘플과 무관하다). 스냅샷 기준일은 2017-08-15다.

- 라인 L1: item I1(A, B가 씀)·I2(A)·I9(신제품, 인스턴스 없음), 라인 L2: item I3(A), 라인 L3: item I8(인스턴스 없음)
- 수주: O1 A/I1 납품 완료, O2 A/I1 납기가 지난 미납분(수주잔고 180), O3 B/I1 앞으로 납기, O4 A/I3 앞으로 납기, O5 기준일 뒤에 들어온 수주
- 계약 단위: item 기본 6, 고객사 B는 12
"""

from pathlib import Path

import pandas as pd

AS_OF = "2017-08-15"

INSTANCES = [
    ("A", "I1", "DAIRY", "food_processing"), ("B", "I1", "DAIRY", "food_processing"), ("A", "I2", "MEATS", "food_processing"),
    ("A", "I3", "BEVERAGES", "beverage_non_alcoholic"),
]
ORDERS = [  # demand_id, company, item, quantity, delivered, order_date, due_date
    ("O1", "A", "I1", 600.0, 600.0, "2017-07-01", "2017-07-20"),
    ("O2", "A", "I1", 300.0, 120.0, "2017-07-20", "2017-08-05"),
    ("O3", "B", "I1", 60.0, 0.0, "2017-08-10", "2017-08-25"),
    ("O4", "A", "I3", 120.0, 0.0, "2017-08-12", "2017-08-20"),
    ("O5", "A", "I2", 60.0, 0.0, "2017-08-20", "2017-08-30"),
]


def write_sample(directory: Path) -> Path:
    """샘플 파일 묶음을 `directory`에 쓰고 그 경로를 돌려준다."""
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "line_assignments.csv": pd.DataFrame(
            [
                {"item_id": item, "line_id": line, "preference": "primary", "valid_from": "2013-01-01"}
                for item, line in [("I1", "L1"), ("I2", "L1"), ("I9", "L1"), ("I3", "L2"), ("I8", "L3")]
            ]
            + [{"item_id": "I2", "line_id": "L2", "preference": "alternate", "valid_from": "2013-01-01"}]
        ),
        "instances.csv": pd.DataFrame(
            [
                {"company_id": c, "item_id": i, "family": f, "industry": ind, "perishable": False, "trait": "smooth",
                 "contract_start": "2013-01-02", "is_synthetic": False}
                for c, i, f, ind in INSTANCES
            ]
        ),
        "orders.csv": pd.DataFrame(
            [
                {"order_date": od, "company_id": c, "item_id": i, "quantity": q, "is_first_order": False, "promotion": None,
                 "is_synthetic": False, "demand_id": d, "due_date": due, "delivered_quantity": delivered}
                for d, c, i, q, delivered, od, due in ORDERS
            ]
        ),
        "manual_quantities.csv": pd.DataFrame(
            [
                {"demand_id": "MAN-0001", "source": "manual", "input_reason": "new_product", "company_id": None, "item_id": "I9",
                 "quantity": 120.0, "entered_at": AS_OF},
                {"demand_id": "MAN-0002", "source": "manual", "input_reason": "promotion", "company_id": "A", "item_id": "I2",
                 "quantity": 60.0, "entered_at": "2017-08-20"},
            ]
        ),
        "min_commitments.csv": pd.DataFrame(
            [{"company_id": "A", "item_id": "I1", "binding": True, "total_quantity": 1200.0, "period_start": "2017-01-01", "period_end": "2017-12-31"}]
        ),
        "supply_guarantees.csv": pd.DataFrame([{"company_id": "A", "item_id": "I2", "quantity": 30.0}]),
        "contract_units.csv": pd.DataFrame(
            [{"item_id": item, "company_id": None, "unit": 6.0} for item in ("I1", "I2", "I3", "I8", "I9")]
            + [{"item_id": None, "company_id": "B", "unit": 12.0}]
        ),
        "inventory.csv": pd.DataFrame([{"item_id": "I1", "quantity": 60.0}, {"item_id": "I2", "quantity": 30.0}, {"item_id": "I3", "quantity": 6.0}]),
        "work_in_progress.csv": pd.DataFrame(
            [{"item_id": "I1", "quantity": 30.0, "demand_ref": "O3"}, {"item_id": "I3", "quantity": 6.0, "demand_ref": "O4"}]
        ),
    }
    for name, frame in files.items():
        frame.to_csv(directory / name, index=False)
    return directory
