from pathlib import Path

import pytest

from sop.favorita_mapping import (
    FAMILY_MAPPING,
    FOOD_PROCESSING_WHOLE_GROUPS,
    in_scope_families,
)

ITEMS_CSV = Path(__file__).resolve().parents[1] / "data" / "raw" / "favorita" / "items.csv"


def test_mapping_covers_all_33_families_with_three_way_split():
    """33개 상품군 = D코드 하나 8개 + 전체 흐름 3개 + 제외 22개."""
    assert len(FAMILY_MAPPING) == 33
    kinds = [m.market_mapping for m in FAMILY_MAPPING.values()]
    assert kinds.count("single_group") == 8
    assert kinds.count("food_processing_whole") == 3
    assert kinds.count("excluded") == 22


@pytest.mark.parametrize(
    "family, industry, group",
    [
        ("MEATS", "food_processing", "D151"),
        ("POULTRY", "food_processing", "D151"),
        ("SEAFOOD", "food_processing", "D151"),
        ("DAIRY", "food_processing", "D152"),
        ("BREAD/BAKERY", "food_processing", "D154"),
        ("PREPARED FOODS", "food_processing", "D154"),
        ("BEVERAGES", "beverage_non_alcoholic", "D155"),
        # 주류·비주류가 한 그룹이라 두 업종에 같은 값
        ("LIQUOR,WINE,BEER", "beverage_alcoholic", "D155"),
    ],
)
def test_single_group_families_map_to_exactly_one_d_code(family, industry, group):
    mapping = FAMILY_MAPPING[family]
    assert mapping.market_mapping == "single_group"
    assert mapping.industry == industry
    assert mapping.market_groups == (group,)


@pytest.mark.parametrize("family", ["GROCERY I", "FROZEN FOODS", "DELI"])
def test_unsplittable_families_use_whole_flow_including_d153(family):
    mapping = FAMILY_MAPPING[family]
    assert mapping.market_mapping == "food_processing_whole"
    assert mapping.industry == "food_processing"
    assert mapping.market_group is None
    assert mapping.market_groups == ("D151", "D152", "D153", "D154")
    assert mapping.market_groups == FOOD_PROCESSING_WHOLE_GROUPS


def test_pet_supplies_and_non_food_are_excluded():
    for family in ("PET SUPPLIES", "PRODUCE", "EGGS", "GROCERY II", "CELEBRATION", "CLEANING"):
        mapping = FAMILY_MAPPING[family]
        assert not mapping.in_scope
        assert mapping.industry is None
        assert mapping.market_groups == ()


def test_in_scope_families_are_the_eleven_mapped_ones():
    assert len(in_scope_families()) == 11


@pytest.mark.skipif(not ITEMS_CSV.exists(), reason="Favorita 원본(data/raw)이 없음")
def test_mapping_matches_families_in_items_csv_exactly():
    import pandas as pd

    families = set(pd.read_csv(ITEMS_CSV)["family"])
    assert families == set(FAMILY_MAPPING)
