"""원가 입력과 과잉·부족 비용 계산 (`cost_estimate` 계산용).

AGENT_NODE_LIST.md forecast agent "입력"의 원가 입력 표를 코드로 옮긴 것이다. 공급가는
시나리오끼리 비교하는 값이라 비율만 있으면 되므로 기준 단위 1로 둔다. 실무에서는
공급가·제조원가·보관비율·위약률을 실제 계약·원가로 바꾼다.

원가는 기간에 따라 바뀌므로 **적용 기간별 값**(`CostPeriod`)으로 두고, 계획 주기마다
그 시점에 유효한 값을 쓴다.
"""

from datetime import date

from pydantic import BaseModel

from .favorita_mapping import Industry

# --- 기본값 (근거는 AGENT_NODE_LIST.md 원가 입력 표) --------------------------------
DEFAULT_SUPPLY_PRICE = 1.0
# NYU Stern Damodaran 업종별 매출총이익률(신흥국, 2017년 회계연도)에서 얻은 원가 비율
DEFAULT_COST_RATIO: dict[Industry, float] = {
    "food_processing": 0.76,
    "beverage_non_alcoholic": 0.60,
    "beverage_alcoholic": 0.46,
}
STORAGE_RATE_PER_MONTH = 0.021  # 제조원가의 월 약 2.1%(연 25%) — 업계 벤치마크 연 20-30%
DEFAULT_SHORTFALL_PENALTY_RATE = 0.03  # 대형 유통사 납품 부족 벌금 사례(Walmart OTIF)
# 원가는 기간별 자료가 없어 전 기간에 같은 값 하나를 쓴다.
DEFAULT_EFFECTIVE_FROM = date(2013, 1, 1)


class CostPeriod(BaseModel, frozen=True):
    industry: Industry
    effective_from: date  # 이 날짜부터 적용
    supply_price: float
    manufacturing_cost: float  # 배송비 포함


class CostSchedule(BaseModel):
    periods: list[CostPeriod]

    def at(self, industry: Industry, on: date) -> CostPeriod:
        """`on` 시점에 유효한 원가 값(적용 시작일이 `on` 이전인 것 중 가장 늦은 것)."""
        candidates = [p for p in self.periods if p.industry == industry and p.effective_from <= on]
        if not candidates:
            raise LookupError(f"{industry}의 {on} 시점에 유효한 원가 값이 없음")
        return max(candidates, key=lambda p: p.effective_from)


def default_cost_schedule() -> CostSchedule:
    return CostSchedule(
        periods=[
            CostPeriod(
                industry=industry,
                effective_from=DEFAULT_EFFECTIVE_FROM,
                supply_price=DEFAULT_SUPPLY_PRICE,
                manufacturing_cost=DEFAULT_SUPPLY_PRICE * ratio,
            )
            for industry, ratio in DEFAULT_COST_RATIO.items()
        ]
    )


def unit_overage_cost(period: CostPeriod, perishable: bool) -> float:
    """1개가 남았을 때의 비용. 신선식품이면 폐기로 제조원가 전체, 아니면 보관비(한 계획 주기)."""
    if perishable:
        return period.manufacturing_cost
    return STORAGE_RATE_PER_MONTH * period.manufacturing_cost


def unit_shortage_cost(period: CostPeriod, shortfall_penalty_rate: float) -> float:
    """1개가 모자랐을 때의 비용 = (공급가 - 제조원가) + 결품 위약금.

    위약금은 주문한 물량의 값(공급가)에 대한 비율로 계산한다 — 결품 위약률은 결품으로
    주문 수량을 다 채우지 못했을 때 계약에 따라 내는 비율이고, 고객사별 계약 조건이라
    호출부가 고객사의 값을 넘긴다.
    """
    return (period.supply_price - period.manufacturing_cost) + (
        shortfall_penalty_rate * period.supply_price
    )
