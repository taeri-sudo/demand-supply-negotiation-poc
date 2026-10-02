"""프로모션 기록 없음 구간의 상승 구간 분류(며칠 튄 날 / 한 달 이상 올랐다 복귀 / 오른 채 유지 / 애매함)."""

import numpy as np
import pandas as pd

from sop.judgment_thresholds import (
    EPISODE_MIN_DAYS,
    PROMO_RETURN_BAND,
    PROMO_SUSTAIN_BAND,
    SPIKE_MAX_DAYS,
    SUSTAIN_DAYS,
)
from sop.promotion_episodes import (
    deseasonalize_weekly,
    find_episodes,
    recorded_patterns,
    weekly_table,
)

START = pd.Timestamp("2013-01-07")  # 월요일 — 주 단위 구간이 정확히 7일씩 나뉜다


def weekly_from(levels, unrecorded_until=None, recorded_promo_weeks=()):
    """주별 판매량 목록으로 주 단위 표를 만든다. unrecorded_until은 기록 없음 주의 개수."""
    n = len(levels)
    unrecorded_until = n if unrecorded_until is None else unrecorded_until
    return pd.DataFrame(
        {
            "week_start": [START + pd.DateOffset(days=7 * k) for k in range(n)],
            "quantity": [float(v) for v in levels],
            "unrecorded": [k < unrecorded_until for k in range(n)],
            "recorded_promo": [k in recorded_promo_weeks for k in range(n)],
        }
    )


def flat(weeks, level=1000.0):
    return [level] * weeks


def test_weekly_table_marks_unrecorded_and_recorded_promo_weeks():
    days = pd.date_range(START, periods=28, freq="D")
    daily = pd.Series(10.0, index=days)
    promotion = pd.Series(pd.array([None] * 14 + [False] * 7 + [True] * 7, dtype="boolean"), index=days)

    table = weekly_table(daily, promotion, days[-1], record_start=days[14])

    assert table["quantity"].tolist() == [70.0] * 4
    assert table["unrecorded"].tolist() == [True, True, False, False]
    assert table["recorded_promo"].tolist() == [False, False, False, True]


def test_a_few_days_spike_is_classified_as_spike_not_promotion():
    levels = flat(12) + [3000.0] + flat(12)  # 한 주(7일) 튐

    [episode] = find_episodes(weekly_from(levels))

    assert episode.days <= SPIKE_MAX_DAYS
    assert episode.kind == "spike"
    assert episode.use_from is None and not episode.ambiguous


def test_month_or_longer_rise_that_returns_is_an_unlabeled_promotion_with_use_from():
    levels = flat(12) + [1800.0] * 6 + flat(12)  # 6주(42일) 올랐다가 원래 수준으로 복귀

    [episode] = find_episodes(weekly_from(levels))

    assert episode.days >= EPISODE_MIN_DAYS
    assert episode.kind == "promotion"
    assert episode.ended and not episode.ambiguous
    assert episode.use_from == (START + pd.DateOffset(days=7 * 18)).date()  # 상승이 끝난 다음 날


def test_rise_that_stays_up_is_a_demand_increase_and_is_kept():
    levels = flat(12) + [1800.0] * (SUSTAIN_DAYS // 7 + 4)  # 끝까지 오른 채 유지

    [episode] = find_episodes(weekly_from(levels))

    assert episode.kind == "sustained"
    assert episode.use_from is None and not episode.ambiguous


def test_rise_that_is_still_running_when_data_ends_is_ambiguous_not_discarded():
    """복귀 여부를 알 수 없고 유지라고 보기엔 이른 경우."""
    levels = flat(12) + [1800.0] * 6  # 6주 올랐는데 데이터가 끝남

    [episode] = find_episodes(weekly_from(levels))

    assert episode.kind == "ambiguous" and episode.ambiguous
    assert episode.use_from is None
    assert "복귀 여부를 알 수 없음" in episode.reason


def test_rise_between_spike_and_month_is_ambiguous():
    levels = flat(12) + [1800.0] * 2 + flat(12)  # 14일 — 며칠도 한 달도 아님

    [episode] = find_episodes(weekly_from(levels))

    assert SPIKE_MAX_DAYS < episode.days < EPISODE_MIN_DAYS
    assert episode.ambiguous and episode.use_from is None


def test_level_after_rise_between_return_and_sustain_bands_is_ambiguous():
    """끝난 뒤 수준이 상승 전보다 +10%와 +25% 사이면 복귀도 유지도 아니다(승인 기준)."""
    levels = flat(12) + [1800.0] * 6 + [1130.0] * 6  # 끝난 뒤 +13%(잡음 여유를 넘지 않아 상승이 끝난 것으로 본다)

    [episode] = find_episodes(weekly_from(levels))

    assert episode.post_level is not None
    after = episode.post_level / episode.baseline - 1
    assert PROMO_RETURN_BAND < after < PROMO_SUSTAIN_BAND
    assert episode.ambiguous and episode.use_from is None


def test_rise_that_is_not_followed_by_enough_weeks_is_ambiguous():
    levels = flat(12) + [1800.0] * 6 + [1000.0]  # 끝난 뒤 관측 1주

    [episode] = find_episodes(weekly_from(levels))

    assert episode.ambiguous and "관측이 부족" in episode.reason


def test_rise_inside_the_recorded_period_is_ignored():
    """기록이 있는 기간의 상승은 프로모션 표시가 이미 붙어 있어 이 처리의 대상이 아니다."""
    levels = flat(12) + [1800.0] * 6 + flat(12)
    table = weekly_from(levels, unrecorded_until=10, recorded_promo_weeks=range(12, 18))

    assert find_episodes(table) == []


def test_promotion_that_differs_from_recorded_patterns_is_flagged_ambiguous():
    """기록 있는 프로모션(2주, +50%)과 기간이 전혀 다른 10주 상승은 같은 종류로 보기 어렵다."""
    levels = flat(12) + [1800.0] * 10 + flat(30) + [1500.0] * 2 + flat(8)
    recorded = {52, 53}
    table = weekly_from(levels, unrecorded_until=40, recorded_promo_weeks=recorded)

    patterns = recorded_patterns(table)
    [episode] = find_episodes(table)

    assert patterns and patterns[0][0] == 14
    assert episode.days == 70
    assert episode.ambiguous and episode.use_from is None
    assert "기록 있는 프로모션" in episode.reason


def test_promotion_matching_recorded_patterns_keeps_use_from():
    levels = flat(12) + [1800.0] * 6 + flat(30) + [1800.0] * 6 + flat(8)
    table = weekly_from(levels, unrecorded_until=40, recorded_promo_weeks=set(range(48, 54)))

    [episode] = find_episodes(table)

    assert episode.kind == "promotion" and episode.use_from is not None


def test_no_recorded_promotions_is_noted_when_comparing_patterns_is_impossible():
    [episode] = find_episodes(weekly_from(flat(12) + [1800.0] * 6 + flat(12)))
    assert episode.kind == "promotion"
    assert any("패턴 비교" in note for note in episode.notes)


def test_count_noise_in_a_low_volume_series_is_not_an_episode():
    """기준 수준이 작은 개수 자료에서 우연한 변동은 상승 구간으로 보지 않는다."""
    rng = np.random.default_rng(3)
    levels = rng.poisson(3, 80)

    kinds = {episode.kind for episode in find_episodes(weekly_from(levels))}

    assert kinds <= {"spike"}  # 한 주 튄 것(오염)은 있어도 프로모션·유지·애매함으로 분류되지 않는다


def test_seasonal_peak_that_repeats_every_year_is_not_an_episode_after_deseasonalizing():
    """2년 이상 이력이 있으면 매년 반복되는 계절 피크를 빼고 보므로 프로모션으로 잡히지 않는다."""
    weeks = np.arange(52 * 4)
    seasonal = np.where((weeks % 52 >= 44) & (weeks % 52 < 50), 800.0, 0.0)  # 매년 6주 피크
    table = weekly_from(1000.0 + seasonal)

    assert any(ep.kind != "spike" for ep in find_episodes(table))  # 그대로 보면 상승 구간으로 잡힌다
    assert find_episodes(deseasonalize_weekly(table)) == []


def test_deseasonalize_leaves_short_history_unchanged():
    table = weekly_from(flat(30))
    pd.testing.assert_frame_equal(deseasonalize_weekly(table), table)
