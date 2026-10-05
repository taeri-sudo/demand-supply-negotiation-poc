"""시장 데이터(INA-R) 출처 등록과 물가 보정.

시장 데이터는 금액(매출액) 기준이라 소비자물가지수로 물가를 보정한다
(AGENT_NODE_LIST.md forecast agent "입력" — 시장 데이터 처리). 원본 시리즈는 그대로
두고 보정값은 계산 결과로 따로 만든다.

여기서는 지수 시리즈까지만 다룬다. 변화율과 "식품 가공 전체 흐름" 평균은
`category_trend.py`에서 만든다.

출처에 우리 회사 매출이 포함돼 있으면(`includes_own_sales`) 시장 전체에서 우리 매출을
빼 "우리를 제외한 시장"으로 쓴다. INA-R은 매출액이 아니라 지수(2002=100)라 우리 매출을
직접 뺄 수 없으므로, 출처 등록 때 지수 1포인트에 해당하는 시장 월 매출액
(`index_unit_amount`)을 받아 우리 매출을 지수 포인트로 환산해 뺀다. 이 값은 데이터에서
나오지 않는 가정값이며, 지수가 계절 조정값일 수 있어 환산은 근사다(DESIGN.md
"실무 전환 시 고려사항" 참고). 차감은 명목 지수에 하고(우리 매출이 명목 금액이므로)
물가 보정은 그 뒤에 한다.
"""

import pandas as pd
from pydantic import BaseModel, model_validator

from .favorita_mapping import Industry


class MarketSource(BaseModel, frozen=True):
    """시장 데이터 출처 등록. 수집 범위를 보고 한 번 정하며, 출처나 범위가 바뀌면 다시 정한다."""

    name: str
    includes_own_sales: bool  # 이 출처에 우리 회사 매출이 포함돼 있는가
    # 지수 1포인트에 해당하는 시장 월 매출액(공급가 단위). includes_own_sales가 true일 때만 필요하다.
    index_unit_amount: float | None = None

    @model_validator(mode="after")
    def _require_unit_amount_when_own_sales_included(self) -> "MarketSource":
        if self.includes_own_sales and (self.index_unit_amount is None or self.index_unit_amount <= 0):
            raise ValueError("includes_own_sales가 true이면 양수의 index_unit_amount가 필요함")
        return self


# 우리 회사는 가상 공급사라 INA-R의 대형 납세자 매출에 포함되지 않는다.
INA_R_SOURCE = MarketSource(name="INA-R", includes_own_sales=False)

# 업종별 물가 보정에 쓰는 IPC(CCIF) 그룹 코드 — 011 Alimentos, 012 Bebidas no
# alcohólicas, 021 Bebidas alcohólicas. 주류 class는 0211(증류주)과 0213(맥주)뿐이라
# 와인이 빠지므로 그룹 단위를 쓴다.
INDUSTRY_PRICE_INDEX: dict[Industry, str] = {
    "food_processing": "011",
    "beverage_non_alcoholic": "012",
    "beverage_alcoholic": "021",
}


def deflate_market_index(
    nominal: pd.Series, price_index: pd.Series, base_month: str | pd.Timestamp
) -> pd.Series:
    """명목 지수를 `base_month`의 물가 수준으로 환산한 실질 지수를 새 시리즈로 반환한다.

    실질_t = 명목_t / (물가_t / 물가_기준월). 입력 시리즈는 바꾸지 않는다. 명목 지수가
    있는 달에 물가지수가 없으면 오류(조용히 보정을 건너뛰지 않는다). 명목이 결측인
    달은 결측으로 남는다.
    """
    base = pd.Timestamp(base_month)
    if base not in price_index.index:
        raise KeyError(f"물가지수에 기준월 {base:%Y-%m}이 없음")
    missing = [m for m in nominal.index[nominal.notna()] if m not in price_index.index]
    if missing:
        raise KeyError(f"물가지수에 없는 달: {[f'{m:%Y-%m}' for m in missing]}")
    relative_price = price_index.reindex(nominal.index) / price_index[base]
    result = nominal / relative_price
    result.name = f"{nominal.name}_real" if nominal.name is not None else "real"
    return result


def own_sales_amount(monthly_quantity: pd.Series, supply_price: float) -> pd.Series:
    """우리 월 매출 = 월 판매 수량 × 공급가."""
    return monthly_quantity * supply_price


def exclude_own_sales(
    source: MarketSource, nominal: pd.Series, own_sales: pd.Series
) -> pd.Series:
    """"우리를 제외한 시장" 명목 지수를 새 시리즈로 반환한다. 입력 시리즈는 바꾸지 않는다.

    우리를 제외한 지수 = 지수 - 우리 월 매출 / `index_unit_amount`. 출처에 우리 매출이
    포함돼 있지 않으면(`includes_own_sales: false`) 우리 매출이 주어져도 차감하지 않고
    그대로 반환한다. `own_sales`에 없는 달은 우리 매출 0으로 본다. 차감 결과가 0 이하이면
    우리 매출이 시장 전체를 넘는다는 뜻이라 `index_unit_amount`가 잘못 정해진 것이므로
    오류를 낸다.
    """
    result = nominal.copy()
    if not source.includes_own_sales:
        return result
    unit_amount = source.index_unit_amount
    if unit_amount is None:  # 등록 단계 검증을 거치지 않고 만든 객체에 대한 방어
        raise ValueError(f"{source.name}: includes_own_sales가 true인데 index_unit_amount가 없어 차감할 수 없음")
    own_points = own_sales.reindex(nominal.index).fillna(0.0) / unit_amount
    result = nominal - own_points
    if (result.dropna() <= 0).any():
        raise ValueError(
            f"{source.name}: 우리 매출이 시장 전체 이상이 돼 차감 결과가 0 이하 — index_unit_amount 확인 필요"
        )
    result.name = nominal.name
    return result
