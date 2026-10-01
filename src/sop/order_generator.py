"""수주 데이터 생성기 — 고객사의 재고 정책을 시뮬레이션해 주문 이력을 만든다.

공개 B2B 수주 데이터가 없어 공개 POS 위에 고객사의 재고 정책을 얹어 우리에게 들어오는
주문을 생성한다(AGENT_NODE_LIST.md "수주 데이터 생성기"). 이 모듈은 고객사의 행동을
대신하는 외부 경계이며, forecast agent는 결과(POS, 주문)만 받는다.

정책은 (s, S) 정기발주다. 점검일마다 고객사는 자기 POS의 직전 `window_days`일
평균·표준편차로 수요를 추정해 재고 위치(보유 + 발주 중)가 s 이하이면 S까지 채우는
주문을 낸다(수량은 MOQ 배수로 올림).

  S = 평균 x (리드타임 + 점검주기) + z x 표준편차 x sqrt(리드타임 + 점검주기)
  s = 평균 x (리드타임 + 점검주기) + 0.5 x z x 표준편차 x sqrt(리드타임 + 점검주기)

s는 다음 점검 전까지의 수요까지 덮는 재주문점이다(리드타임만 덮으면 점검 주기 사이에
재고가 바닥나 판매 손실이 반복된다). 수요가 불규칙한 상품은 표준편차가 커서 s가 S보다
충분히 낮아져, 재고 위치가 s 아래로 내려갈 때까지 주문이 건너뛰어지고 MOQ 단위로 묶여 나간다.

POS와 주문의 관계가 완벽하면 forecast가 POS로 주문을 역산해 예측 정확도가
부풀려지므로, 다음 불완전성을 일부러 넣는다: 정책 파라미터(z, 추정 창)의 시기별 변화,
점검일 흔들림과 건너뜀, 수동 조정 주문, 프로모션 중 S 상향, 계약 시작일의 첫 주문(초기
진열·재고 물량이라 평소보다 큼). 같은 입력과 시드면 같은 결과가 나온다.
"""

import math
import zlib
from datetime import date

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

# --- 불완전성 상수 (한 곳에 모아 조정) ----------------------------------------------
REVIEW_JITTER_DAYS = 1  # 점검일이 정해진 날에서 -1일에서 +1일 사이로 흔들림
REVIEW_SKIP_PROB = 0.05  # 점검 자체를 건너뛸 확률
MANUAL_ADJUSTMENT_PROB = 0.04  # 수동으로 수량을 바꾸는 주문 비율
MANUAL_ADJUSTMENT_MULTIPLIERS = (0.5, 1.5, 2.0)
REORDER_POINT_SAFETY_SHARE = 0.5  # s에 반영하는 안전재고 비율(S는 전부 반영)
PROMOTION_S_MULTIPLIER = 1.25  # 프로모션 중에는 S(와 s)를 올린다
FIRST_ORDER_MULTIPLIER = 2.5  # 계약 시작일 첫 주문은 S의 2.5배(초기 진열·재고)
PROMOTION_MARK_WINDOW_DAYS = 7  # 직전 7일 안에 프로모션 판매일이 있으면 주문에 프로모션 표시


class PolicyChange(BaseModel, frozen=True):
    effective_from: date
    z: float
    window_days: int


class CustomerPolicy(BaseModel):
    company_id: str
    review_days: int
    lead_time_days: int
    moq: float
    z: float
    window_days: int = 28
    changes: list[PolicyChange] = Field(default_factory=list)

    def params_at(self, day: date) -> tuple[float, int]:
        """`day`에 적용되는 (z, 추정 창 일수). 시기별 변화 중 가장 늦은 것이 이긴다."""
        z, window = self.z, self.window_days
        for change in sorted(self.changes, key=lambda c: c.effective_from):
            if change.effective_from <= day:
                z, window = change.z, change.window_days
        return z, window


def stable_seed(base_seed: int, company_id: str, item_id: str) -> int:
    """(고객사, item)마다 다르지만 실행마다 같은 시드."""
    return zlib.crc32(f"{base_seed}|{company_id}|{item_id}".encode())


def ceil_to_moq(quantity: float, moq: float) -> float:
    return math.ceil(round(quantity / moq, 9)) * moq


def promotion_marks(pos: pd.DataFrame, index: pd.DatetimeIndex) -> pd.Series:
    """날짜별 주문 프로모션 표시 — true/false/null.

    직전 `PROMOTION_MARK_WINDOW_DAYS`일 안에 프로모션 판매일이 있으면 true. POS에서
    프로모션 기록이 시작되기 전 날짜는 null(기록 없음)이며 false로 채우지 않는다.
    """
    recorded = pos["promotion"].dropna()
    if recorded.empty:
        return pd.Series(pd.array([None] * len(index), dtype="boolean"), index=index)
    record_start = pos.loc[recorded.index, "date"].min()
    promo_days = (
        pos.loc[pos["promotion"].eq(True).fillna(False), "date"].drop_duplicates().to_numpy(dtype="datetime64[D]")
    )
    flags = pd.Series(False, index=index)
    if len(promo_days):
        marked = pd.Series(1, index=pd.DatetimeIndex(promo_days)).reindex(index, fill_value=0)
        flags = marked.rolling(PROMOTION_MARK_WINDOW_DAYS, min_periods=1).max().astype(bool)
    values = [None if d < record_start else bool(f) for d, f in zip(index, flags)]
    return pd.Series(pd.array(values, dtype="boolean"), index=index)


def generate_orders(
    pos: pd.DataFrame,
    policy: CustomerPolicy,
    item_id: str,
    contract_start: date,
    end: date,
    base_seed: int = 0,
) -> pd.DataFrame:
    """한 (고객사, item)의 주문 이력을 생성한다.

    `pos`는 그 고객사·item의 일별 POS(열: date, quantity, promotion). 음수 판매량(반품)은
    재고 소진에 반영하지 않는다. 반환 열: order_date, company_id, item_id, quantity,
    is_first_order, promotion(true/false/null), is_synthetic(항상 False).
    """
    rng = np.random.default_rng(stable_seed(base_seed, policy.company_id, item_id))
    start, stop = pd.Timestamp(contract_start), pd.Timestamp(end)
    first_day = min(pos["date"].min(), start)
    days = pd.date_range(first_day, stop, freq="D")
    demand = pos.groupby("date")["quantity"].sum().clip(lower=0).reindex(days, fill_value=0.0)
    promo = promotion_marks(pos, days)

    rolling = {
        w: (demand.rolling(w, min_periods=1).mean(), demand.rolling(w, min_periods=1).std().fillna(0.0))
        for w in {policy.window_days, *(c.window_days for c in policy.changes)}
    }

    def order_up_to_levels(day: pd.Timestamp) -> tuple[float, float]:
        z, window = policy.params_at(day.date())
        mean, std = rolling[window][0][day], rolling[window][1][day]
        lead, horizon = policy.lead_time_days, policy.lead_time_days + policy.review_days
        big_s = mean * horizon + z * std * math.sqrt(horizon)
        s_level = mean * horizon + REORDER_POINT_SAFETY_SHARE * z * std * math.sqrt(horizon)
        if not pd.isna(promo[day]) and bool(promo[day]):
            s_level, big_s = s_level * PROMOTION_S_MULTIPLIER, big_s * PROMOTION_S_MULTIPLIER
        return s_level, big_s

    # 점검일: 정해진 주기에서 흔들리거나 건너뛴 실제 점검일
    review_days: set[pd.Timestamp] = set()
    nominal = start + pd.DateOffset(days=policy.review_days)
    while nominal <= stop:
        if rng.random() >= REVIEW_SKIP_PROB:
            jitter = int(rng.integers(-REVIEW_JITTER_DAYS, REVIEW_JITTER_DAYS + 1))
            actual = nominal + pd.DateOffset(days=jitter)
            if start < actual <= stop:
                review_days.add(actual)
        nominal += pd.DateOffset(days=policy.review_days)

    records: list[dict] = []

    def record(day: pd.Timestamp, quantity: float, first: bool) -> None:
        mark = promo[day]
        records.append(
            {
                "order_date": day,
                "company_id": policy.company_id,
                "item_id": item_id,
                "quantity": quantity,
                "is_first_order": first,
                "promotion": None if pd.isna(mark) else bool(mark),
                "is_synthetic": False,
            }
        )

    # 계약 시작일: 첫 주문은 즉시 입고되는 초기 진열·재고 물량
    _, big_s0 = order_up_to_levels(start)
    first_quantity = max(policy.moq, ceil_to_moq(FIRST_ORDER_MULTIPLIER * big_s0, policy.moq))
    on_hand = first_quantity
    record(start, first_quantity, True)
    pipeline: list[tuple[pd.Timestamp, float]] = []

    for day in pd.date_range(start, stop, freq="D"):
        arrived = [q for arrival, q in pipeline if arrival <= day]
        if arrived:
            on_hand += sum(arrived)
            pipeline = [(a, q) for a, q in pipeline if a > day]
        if day in review_days:
            s_level, big_s = order_up_to_levels(day)
            position = on_hand + sum(q for _, q in pipeline)
            if big_s > 0 and position <= s_level and big_s > position:
                quantity = ceil_to_moq(big_s - position, policy.moq)
                if rng.random() < MANUAL_ADJUSTMENT_PROB:
                    factor = float(rng.choice(MANUAL_ADJUSTMENT_MULTIPLIERS))
                    quantity = max(policy.moq, ceil_to_moq(quantity * factor, policy.moq))
                record(day, quantity, False)
                pipeline.append((day + pd.DateOffset(days=policy.lead_time_days), quantity))
        on_hand = max(0.0, on_hand - float(demand[day]))

    orders = pd.DataFrame.from_records(records)
    orders["promotion"] = pd.array(orders["promotion"].tolist(), dtype="boolean")
    return orders
