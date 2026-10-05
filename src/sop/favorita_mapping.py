"""Favorita 상품군 33개의 업종·INA-R 시장 데이터 매핑 표.

AGENT_NODE_LIST.md "외부 경계" 데이터 대체 가정의 매핑 표를 코드로 옮긴 것이다
(값이 바뀌면 그 문서의 표와 이 파일을 함께 고친다). 상품군은 세 경우 중 하나다.

- `single_group`: 상품군 하나로 INA-R 3자리 그룹(D코드) 하나를 특정할 수 있음 —
  그 D코드의 월별 시리즈를 그대로 쓴다(가중·평균 없음).
- `food_processing_whole`: 상품군 안에 여러 그룹이 섞여 있어 D코드 하나로 특정할
  수 없음 — D151, D152, D153, D154를 합친 "식품 가공 전체 흐름"을 쓴다. 이 흐름의
  변화율은 그룹별로 만든 뒤 단순 평균한다(`category_trend.py`). 여기서는 대응하는
  D코드 목록만 둔다.
- `excluded`: 우리 회사 제품 범위(식품 가공, 음료(비주류), 음료(주류)) 밖.

한계(라벨 기준 근사 매핑, 단순 평균은 네 코드 각 25%라는 중립 가정, PET SUPPLIES
제외)는 DESIGN.md "실무 전환 시 고려사항" 참고.
"""

from typing import Literal

from pydantic import BaseModel

Industry = Literal["food_processing", "beverage_non_alcoholic", "beverage_alcoholic"]
MarketMapping = Literal["single_group", "food_processing_whole", "excluded"]

# "식품 가공 전체 흐름"을 이루는 INA-R 그룹. 비중을 알 수 없어 각 코드를 동등하게
# 취급한다(변화율을 그룹별로 만든 뒤 단순 평균).
FOOD_PROCESSING_WHOLE_GROUPS: tuple[str, ...] = ("D151", "D152", "D153", "D154")


class FamilyMapping(BaseModel, frozen=True):
    family: str
    industry: Industry | None
    market_mapping: MarketMapping
    market_group: str | None  # market_mapping이 single_group일 때만 D코드

    @property
    def in_scope(self) -> bool:
        return self.market_mapping != "excluded"

    @property
    def market_groups(self) -> tuple[str, ...]:
        """이 상품군의 시장 흐름을 이루는 INA-R 그룹 코드(제외 상품군은 빈 튜플)."""
        group = self.market_group
        if self.market_mapping == "single_group" and group is not None:
            return (group,)
        if self.market_mapping == "food_processing_whole":
            return FOOD_PROCESSING_WHOLE_GROUPS
        return ()


def _single(family: str, industry: Industry, group: str) -> FamilyMapping:
    return FamilyMapping(
        family=family, industry=industry, market_mapping="single_group", market_group=group
    )


def _whole(family: str) -> FamilyMapping:
    return FamilyMapping(
        family=family,
        industry="food_processing",
        market_mapping="food_processing_whole",
        market_group=None,
    )


def _excluded(family: str) -> FamilyMapping:
    return FamilyMapping(
        family=family, industry=None, market_mapping="excluded", market_group=None
    )


_IN_SCOPE = [
    _single("MEATS", "food_processing", "D151"),
    _single("POULTRY", "food_processing", "D151"),
    _single("SEAFOOD", "food_processing", "D151"),
    _single("DAIRY", "food_processing", "D152"),
    _single("BREAD/BAKERY", "food_processing", "D154"),
    _single("PREPARED FOODS", "food_processing", "D154"),
    _whole("GROCERY I"),
    _whole("FROZEN FOODS"),
    _whole("DELI"),
    # 주류·비주류가 INA-R에서 한 그룹(D155)이라 두 업종에 같은 값이 들어간다.
    _single("BEVERAGES", "beverage_non_alcoholic", "D155"),
    _single("LIQUOR,WINE,BEER", "beverage_alcoholic", "D155"),
]

# 범위 밖: 비식품 전부와, 식품이어도 제조업 그룹으로 특정할 수 없는 신선·행사 상품군.
# PET SUPPLIES(사료 등 D153 성격 품목)도 비식품 범위 밖이라 여기 속한다.
_EXCLUDED_FAMILIES = [
    "AUTOMOTIVE",
    "BABY CARE",
    "BEAUTY",
    "BOOKS",
    "CELEBRATION",
    "CLEANING",
    "EGGS",
    "GROCERY II",
    "HARDWARE",
    "HOME AND KITCHEN I",
    "HOME AND KITCHEN II",
    "HOME APPLIANCES",
    "HOME CARE",
    "LADIESWEAR",
    "LAWN AND GARDEN",
    "LINGERIE",
    "MAGAZINES",
    "PERSONAL CARE",
    "PET SUPPLIES",
    "PLAYERS AND ELECTRONICS",
    "PRODUCE",
    "SCHOOL AND OFFICE SUPPLIES",
]

FAMILY_MAPPING: dict[str, FamilyMapping] = {
    m.family: m for m in [*_IN_SCOPE, *(_excluded(f) for f in _EXCLUDED_FAMILIES)]
}


def in_scope_families() -> list[str]:
    return [family for family, m in FAMILY_MAPPING.items() if m.in_scope]
