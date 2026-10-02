"""로컬 고정 샘플 생성 스크립트 (M2 2단계).

Favorita 원본(`data/raw/favorita/`)에서 고객사 6곳과 우리 회사 제품 범위(`FAMILY_MAPPING`의
범위 안 상품군)의 일별 POS를 읽어 (고객사, item) 인스턴스를 고르고, 그 위에 수주 생성기로
주문 이력을 만들어 `data/generated/`에 고정 파일로 저장한다. 원본과 같이 저장소에는
포함하지 않는다(Kaggle 대회 데이터 재배포 조건 — DESIGN.md "데이터 출처"). 고정 시드로
같은 파일을 다시 만들 수 있으나, 생성기나 파라미터를 바꾸면 결과가 달라지므로 그때는 다시
만들어야 한다(AGENT_NODE_LIST.md "수주 데이터 생성기" 조건 4).

실행: `python -m sop.sample_builder` (저장소 루트에서, PYTHONPATH=src). 원본 train.csv를
한 번 끝까지 읽으므로 몇 분 걸린다. 같은 입력과 시드면 같은 파일이 나온다.
"""

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .cost_inputs import DEFAULT_SHORTFALL_PENALTY_RATE
from .favorita_loader import (
    aggregate_monthly,
    load_items,
    scan_pos,
    store_to_company_id,
    to_boundary_format,
)
from .order_generator import CustomerPolicy, PolicyChange, generate_orders

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "raw" / "favorita"
OUT_DIR = REPO_ROOT / "data" / "generated"

BASE_SEED = 20260930
# 원본 train.csv의 마지막 날짜(2017-08-15). 일부 매장·상품만 읽어도 완전한 달을 같은
# 기준으로 거르려고 고정값으로 둔다.
DATA_END = pd.Timestamp("2017-08-15")
LAST_FULL_MONTH = pd.Timestamp("2017-07-01")  # DATA_END가 속한 달은 중간에 끝나 제외

# 고객사 6곳: 유형(A-D)·지역을 섞고, 매장 20은 2015년 2월에 문을 열어 POS 이력이 짧다.
SAMPLE_STORES = [44, 3, 34, 37, 14, 20]

# 고객사별 재고 정책(생성기 내부 값 — forecast agent는 접근하지 않는다)과 계약 조건
CUSTOMER_POLICIES: dict[int, CustomerPolicy] = {
    44: CustomerPolicy(
        company_id=store_to_company_id(44), review_days=7, lead_time_days=2, moq=12, z=1.65,
        changes=[PolicyChange(effective_from=date(2015, 6, 1), z=1.28, window_days=56)],
    ),
    3: CustomerPolicy(
        company_id=store_to_company_id(3), review_days=7, lead_time_days=3, moq=6, z=1.28,
        changes=[PolicyChange(effective_from=date(2014, 9, 1), z=1.65, window_days=28)],
    ),
    34: CustomerPolicy(
        company_id=store_to_company_id(34), review_days=14, lead_time_days=3, moq=24, z=1.0,
        changes=[
            PolicyChange(effective_from=date(2014, 3, 1), z=1.28, window_days=56),
            PolicyChange(effective_from=date(2016, 5, 1), z=1.0, window_days=28),
        ],
    ),
    37: CustomerPolicy(
        company_id=store_to_company_id(37), review_days=7, lead_time_days=4, moq=12, z=1.5,
        changes=[PolicyChange(effective_from=date(2016, 1, 1), z=1.0, window_days=56)],
    ),
    14: CustomerPolicy(
        company_id=store_to_company_id(14), review_days=14, lead_time_days=5, moq=6, z=1.28,
        changes=[PolicyChange(effective_from=date(2015, 1, 1), z=1.65, window_days=28)],
    ),
    20: CustomerPolicy(
        company_id=store_to_company_id(20), review_days=7, lead_time_days=2, moq=12, z=1.28,
    ),
}
CONTRACT_PENALTY_RATES = {44: 0.05, 3: 0.03, 34: 0.02, 37: 0.03, 14: 0.04, 20: 0.03}

# 인스턴스 선택 계획: (상품군, 특성, 개수). 특성은 pair_metrics 기준.
PLAN: list[tuple[str, str, int]] = [
    ("BEVERAGES", "smooth", 2),
    ("DAIRY", "smooth", 1),
    ("BREAD/BAKERY", "smooth", 1),
    ("MEATS", "smooth", 1),
    ("POULTRY", "smooth", 1),
    ("GROCERY I", "smooth", 1),
    ("LIQUOR,WINE,BEER", "seasonal", 2),
    ("BEVERAGES", "seasonal", 1),
    ("FROZEN FOODS", "sparse", 1),
    ("DELI", "sparse", 1),
    ("PREPARED FOODS", "sparse", 1),
]
LATE_CONTRACT_START = {"A": date(2016, 10, 1), "B": date(2017, 1, 1), "C": date(2017, 3, 1)}
MIN_MONTHS_FULL = 48  # 이력이 긴 인스턴스의 최소 완전한 달 수
MIN_MONTHS_LATE = 24  # 늦게 계약하는 인스턴스는 그 매장의 POS가 이 이상이면 허용
SIMILAR_ITEMS_PER_TARGET = 2  # 같은 매장·같은 class의 비슷한 제품


def monthly_series(daily: pd.DataFrame) -> pd.DataFrame:
    """(store_nbr, item_nbr, month)별 완전한 달 판매량."""
    return aggregate_monthly(daily, ["store_nbr", "item_nbr"], data_end=DATA_END)


def pair_metrics(monthly: pd.DataFrame) -> pd.DataFrame:
    """(매장, 상품)별 특성: 완전한 달 수, 월 평균, 판매일 비율, 계절성 강도, 첫 달.

    판매량이 없는 달(행 없음)은 0으로 채운 뒤 계산한다. 계절성 강도는 선형 추세를 뺀
    월 시리즈에서 달력월 평균이 설명하는 분산 비율이다.
    """
    rows = []
    for (store, item), g in monthly.groupby(["store_nbr", "item_nbr"]):
        first = g["month"].min()
        index = pd.date_range(first, LAST_FULL_MONTH, freq="MS")
        q = g.set_index("month")["quantity"].reindex(index, fill_value=0.0).to_numpy(dtype=float)
        n = len(q)
        t = np.arange(n)
        residual = q - np.polyval(np.polyfit(t, q, 1), t) if n > 2 else q - q.mean()
        total_var = residual.var()
        months = np.array([m.month for m in index])
        explained = (
            sum((months == k).sum() * residual[months == k].mean() ** 2 for k in range(1, 13) if (months == k).any())
            / n
        )
        calendar_days = sum(m.days_in_month for m in index)
        rows.append(
            {
                "store_nbr": store,
                "item_nbr": item,
                "first_month": first,
                "n_months": n,
                "mean_monthly": q.mean(),
                "sales_day_fraction": g["sales_days"].sum() / calendar_days,
                "zero_month_fraction": float((q == 0).mean()),
                "seasonal_strength": explained / total_var if total_var > 0 else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _rank(metrics: pd.DataFrame, trait: str) -> pd.DataFrame:
    if trait == "smooth":
        eligible = metrics[(metrics["zero_month_fraction"] == 0) & (metrics["sales_day_fraction"] > 0.9)]
        return eligible.sort_values(["mean_monthly", "store_nbr", "item_nbr"], ascending=[False, True, True])
    if trait == "seasonal":
        eligible = metrics[(metrics["mean_monthly"] >= 100) & (metrics["zero_month_fraction"] == 0)]
        return eligible.sort_values(["seasonal_strength", "store_nbr", "item_nbr"], ascending=[False, True, True])
    if trait == "sparse":
        eligible = metrics[(metrics["sales_day_fraction"] > 0.1) & (metrics["sales_day_fraction"] < 0.35)]
        return eligible.assign(_gap=(eligible["sales_day_fraction"] - 0.2).abs()).sort_values(
            ["_gap", "store_nbr", "item_nbr"]
        )
    raise ValueError(f"알 수 없는 특성: {trait}")


def choose_pairs(metrics: pd.DataFrame, items: pd.DataFrame, plan=PLAN) -> list[dict]:
    """계획(상품군, 특성, 개수)대로 (매장, 상품)을 고른다. 같은 (매장, 상품)을 두 번 고르지 않는다."""
    family = items.set_index("item_nbr")["family"]
    enriched = metrics.assign(family=metrics["item_nbr"].map(family))
    long_history = enriched[enriched["n_months"] >= MIN_MONTHS_FULL]
    chosen: list[dict] = []
    taken: set[tuple[int, int]] = set()
    for fam, trait, count in plan:
        pool = _rank(long_history[long_history["family"] == fam], trait)
        picked = 0
        for row in pool.itertuples():
            key = (int(row.store_nbr), int(row.item_nbr))  # pyright: ignore[reportArgumentType] -- itertuples 값이 Scalar로 추론되나 실제는 정수
            if key in taken:
                continue
            taken.add(key)
            chosen.append(
                {"store_nbr": key[0], "item_nbr": key[1], "family": fam, "trait": trait, "late_start": None}
            )
            picked += 1
            if picked == count:
                break
        if picked < count:
            raise ValueError(f"{fam}/{trait}: 조건에 맞는 (매장, 상품)이 {count}개 미만")
    return chosen


def add_late_contract_pairs(chosen: list[dict], metrics: pd.DataFrame) -> list[dict]:
    """smooth 인스턴스 3개의 같은 상품을 다른 매장의 늦은 신규 계약으로 추가한다(이력이 짧은 인스턴스)."""
    extras: list[dict] = []
    taken = {(c["store_nbr"], c["item_nbr"]) for c in chosen}
    smooth_first = [c for c in chosen if c["trait"] == "smooth"][: len(LATE_CONTRACT_START)]
    for (label, start), base in zip(LATE_CONTRACT_START.items(), smooth_first):
        candidates = metrics[
            (metrics["item_nbr"] == base["item_nbr"])
            & (metrics["store_nbr"] != base["store_nbr"])
            & (metrics["n_months"] >= MIN_MONTHS_LATE)
        ].sort_values(["n_months", "mean_monthly"], ascending=[False, False])
        for row in candidates.itertuples():
            key = (int(row.store_nbr), int(row.item_nbr))
            if key in taken:
                continue
            taken.add(key)
            extras.append({**base, "store_nbr": key[0], "trait": "short_history", "late_start": start})
            break
        else:
            raise ValueError(f"짧은 이력 인스턴스를 만들 다른 매장이 없음: item {base['item_nbr']}")
    return [*chosen, *extras]


def similar_items(
    chosen: list[dict], items: pd.DataFrame, monthly: pd.DataFrame
) -> dict[tuple[int, int], list[int]]:
    """각 인스턴스(매장, 상품)와 같은 매장·같은 class에서 판매량이 큰 다른 상품(보강용 similar_item POS)."""
    class_of = items.set_index("item_nbr")["class_id"]
    volume = monthly.groupby(["store_nbr", "item_nbr"])["quantity"].sum()
    result: dict[tuple[int, int], list[int]] = {}
    for c in chosen:
        store, item = c["store_nbr"], c["item_nbr"]
        same_class = items[(items["class_id"] == class_of[item]) & (items["item_nbr"] != item)]["item_nbr"]
        candidates = [
            (volume.get((store, i), 0.0), i) for i in same_class if (store, i) in volume.index
        ]
        result[(store, item)] = [int(i) for _, i in sorted(candidates, reverse=True)[:SIMILAR_ITEMS_PER_TARGET]]
    return result


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, date_format="%Y-%m-%d", lineterminator="\n")


def build_sample(
    train_path: Path = RAW_DIR / "train.csv",
    items_path: Path = RAW_DIR / "items.csv",
    out_dir: Path = OUT_DIR,
    cache_path: Path | None = None,
) -> dict:
    """샘플 파일을 생성해 `out_dir`에 쓰고 요약(dict)을 반환한다."""
    items = load_items(items_path)
    scope_items = items[items["in_scope"]]
    if cache_path is not None and cache_path.exists():
        daily = pd.read_parquet(cache_path)
        daily["promotion"] = daily["promotion"].astype("boolean")
    else:
        daily = scan_pos(train_path, set(SAMPLE_STORES), set(scope_items["item_nbr"]))
        if cache_path is not None:
            daily.to_parquet(cache_path)
    print(f"[sample_builder:build_sample] daily_rows={len(daily)}")

    monthly = monthly_series(daily)
    metrics = pair_metrics(monthly)
    chosen = add_late_contract_pairs(choose_pairs(metrics, items), metrics)
    similar_map = similar_items(chosen, items, monthly)
    extra_pairs = {(store, i) for (store, _), ids in similar_map.items() for i in ids}
    target_pairs = {(c["store_nbr"], c["item_nbr"]) for c in chosen}

    keep = target_pairs | extra_pairs
    pair_index = pd.MultiIndex.from_frame(daily[["store_nbr", "item_nbr"]])
    kept = daily[pair_index.isin(list(keep))]

    # 주문 이력 생성
    order_frames, instance_rows = [], []
    item_info = items.set_index("item_nbr")
    for c in chosen:
        policy = CUSTOMER_POLICIES[c["store_nbr"]]
        pos = kept[(kept["store_nbr"] == c["store_nbr"]) & (kept["item_nbr"] == c["item_nbr"])]
        first_pos = pos["date"].min().date()
        start = c["late_start"] or max(date(2013, 1, 1), first_pos)
        item_id = item_info.loc[c["item_nbr"], "item_id"]
        orders = generate_orders(
            pos[["date", "quantity", "promotion"]], policy, item_id, start, DATA_END.date(), BASE_SEED  # pyright: ignore[reportArgumentType] -- .loc 조회값이 Scalar로 추론되나 실제는 str
        )
        order_frames.append(orders)
        instance_rows.append(
            {
                "company_id": policy.company_id,
                "item_id": item_id,
                "family": c["family"],
                "industry": item_info.loc[c["item_nbr"], "industry"],
                "perishable": bool(item_info.loc[c["item_nbr"], "perishable"]),
                "trait": c["trait"],
                "contract_start": start.isoformat(),
                "is_synthetic": False,
            }
        )
    orders_all = pd.concat(order_frames, ignore_index=True).sort_values(
        ["company_id", "item_id", "order_date"]
    )

    # 고객사 계약 조건과 내부 정책
    used_stores = sorted({c["store_nbr"] for c in chosen}, key=SAMPLE_STORES.index)
    contracts = pd.DataFrame(
        [
            {
                "company_id": CUSTOMER_POLICIES[s].company_id,
                "moq": CUSTOMER_POLICIES[s].moq,
                "shortfall_penalty_rate": CONTRACT_PENALTY_RATES.get(s, DEFAULT_SHORTFALL_PENALTY_RATE),
            }
            for s in used_stores
        ]
    )

    # 상품군 단위 월별 POS(category 범위)
    family_of = items.set_index("item_nbr")["family"]
    category_daily = daily.assign(family=daily["item_nbr"].map(family_of))
    category = aggregate_monthly(category_daily, ["store_nbr", "family"], data_end=DATA_END)
    category.insert(0, "company_id", category["store_nbr"].map(store_to_company_id))
    category = category.drop(columns=["store_nbr"])

    item_id_of = items.set_index("item_nbr")["item_id"]
    similar_rows = pd.DataFrame(
        [
            {
                "company_id": store_to_company_id(store),
                "item_id": item_id_of[target],
                "similar_item_id": item_id_of[i],
            }
            for (store, target), ids in similar_map.items()
            for i in ids
        ]
    )

    pos_out = to_boundary_format(kept).sort_values(["company_id", "item_id", "date"])
    pos_out["role"] = [
        "target" if (s, i) in target_pairs else "similar"
        for s, i in zip(kept.loc[pos_out.index, "store_nbr"], kept.loc[pos_out.index, "item_nbr"])
    ]

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(pos_out, out_dir / "pos_daily.csv")
    _write_csv(category, out_dir / "pos_category_monthly.csv")
    _write_csv(orders_all, out_dir / "orders.csv")
    _write_csv(contracts, out_dir / "contracts.csv")
    _write_csv(similar_rows, out_dir / "similar_items.csv")
    _write_csv(pd.DataFrame(instance_rows), out_dir / "instances.csv")
    meta = {
        "data_end": DATA_END.date().isoformat(),
        "base_seed": BASE_SEED,
        "stores": SAMPLE_STORES,
        "note": "orders.csv는 수주 생성기의 결과이며 is_synthetic=true 행은 가공 시리즈다.",
    }
    (out_dir / "sample_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 생성기 내부 정책 — forecast agent가 읽는 파일이 아니다(재현용 기록)
    policies = {s: CUSTOMER_POLICIES[s].model_dump(mode="json") for s in used_stores}
    (out_dir / "generator_params.json").write_text(
        json.dumps(policies, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"instances": len(chosen), "pos_rows": len(pos_out), "orders": len(orders_all)}


if __name__ == "__main__":
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    print(build_sample(cache_path=cache))
