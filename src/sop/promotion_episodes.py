"""프로모션 기록 없음 구간의 상승 구간 분류.

AGENT_NODE_LIST.md forecast agent 2단계: 프로모션 여부가 기록되지 않은 기간에는 표시 없이
섞인 프로모션이 있을 수 있다. 같은 고객사·item의 일별 POS를 주 단위로 모아 상승 구간을 찾고,
상승이 지속된 기간과 끝난 뒤 수준으로 다음 넷 중 하나로 분류한다.

- `spike`: 며칠(SPIKE_MAX_DAYS 이하) 튄 구간 — 오염으로 보고 정제 대상이다.
- `promotion`: 한 달 이상(EPISODE_MIN_DAYS 이상) 올랐다가 원래 수준(상승 전 대비 +10% 이내)으로
  돌아온 구간 — 표시 없는 프로모션으로 보고 그 구간이 끝난 뒤부터 쓴다(`use_from`).
- `sustained`: 오른 채 유지(끝난 뒤에도 상승 전 대비 +25% 이상이거나 SUSTAIN_DAYS 이상 내려오지
  않음) — 실제 수요 증가로 보고 그대로 쓴다.
- `ambiguous`: 위 어느 쪽인지 구분이 안 되는 경우(기간이 spike와 promotion 사이, 끝난 뒤
  수준이 +10%와 +25% 사이, 끝나는지 알 수 없음, 기록 있는 프로모션 패턴과 다름) — 표시만 하고
  데이터는 버리지 않는다.

같은 시기 시장 데이터와 비교하는 근거는 시장 변화율이 필요해 이 모듈에 없다(변화율 기준은
M2 4단계 전에 정해진다). 상승 구간은 기록이 시작되기 전의 주에서 시작한 것만 본다 — 기록이
있는 기간의 상승은 프로모션 표시가 이미 붙어 있다.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from . import stats_adapter
from .judgment_thresholds import (
    BASELINE_WEEKS,
    EPISODE_MIN_DAYS,
    EPISODE_NOISE_K,
    EPISODE_UPLIFT_MIN,
    MIN_POST_WEEKS,
    POST_WEEKS,
    PROMO_PATTERN_TOLERANCE,
    PROMO_RETURN_BAND,
    PROMO_SUSTAIN_BAND,
    SPIKE_MAX_DAYS,
    SUSTAIN_DAYS,
    WEEKLY_SEASON_PERIOD,
)


@dataclass
class Episode:
    start: pd.Timestamp  # 상승 구간의 첫 날
    end: pd.Timestamp  # 상승 구간이 끝난 다음 날(끝나지 않았으면 데이터 끝 다음 날)
    days: int
    baseline: float  # 상승 전 기준 수준(주 합계 중앙값)
    run_level: float
    ended: bool
    post_level: float | None
    kind: str  # spike | promotion | sustained | ambiguous
    reason: str
    ambiguous: bool = False
    use_from: date | None = None
    notes: list[str] = field(default_factory=list)


def weekly_table(
    daily: pd.Series, promotion: pd.Series, data_end: pd.Timestamp, record_start: pd.Timestamp | None
) -> pd.DataFrame:
    """일별 판매량(결측일은 0으로 채운 정제본)을 7일 단위로 모은다(완전한 주만).

    열: week_start, quantity, unrecorded(그 주가 프로모션 기록 시작 전), recorded_promo(그 주에
    기록된 프로모션 판매일이 있음).
    """
    start = daily.index.min()
    n_weeks = ((pd.Timestamp(data_end) - start).days + 1) // 7
    rows = []
    for k in range(n_weeks):
        first = start + pd.DateOffset(days=7 * k)
        last = first + pd.DateOffset(days=6)
        window = daily.loc[first:last]
        flags = promotion.loc[first:last]
        rows.append(
            {
                "week_start": first,
                "quantity": float(window.sum()),
                "unrecorded": record_start is None or last < record_start,
                "recorded_promo": bool(flags.eq(True).any()),
            }
        )
    return pd.DataFrame(rows)


def deseasonalize_weekly(weekly: pd.DataFrame) -> pd.DataFrame:
    """이력이 2주기 이상이면 주별 판매량에서 계절 성분을 빼 정상적인 계절 피크가 상승 구간으로 잡히지 않게 한다.

    이력이 짧으면 계절성을 알 수 없어 그대로 두며, 그 경우 계절 피크와 표시 없는 프로모션을 구분하지
    못할 수 있다(구분이 안 되면 "애매함" 표시로 이어진다).
    """
    parts = stats_adapter.decompose(weekly["quantity"].to_numpy(), WEEKLY_SEASON_PERIOD)
    if parts is None:
        return weekly
    adjusted = weekly.copy()
    adjusted["quantity"] = np.clip(weekly["quantity"].to_numpy() - parts.seasonal, 0.0, None)
    return adjusted


def recorded_patterns(weekly: pd.DataFrame) -> list[tuple[int, float]]:
    """기록 있는 프로모션 구간의 (기간 일수, 증가폭) 목록. 증가폭은 직전 기준 수준 대비 비율."""
    x = weekly["quantity"].to_numpy()
    flagged = weekly["recorded_promo"].to_numpy()
    patterns = []
    i = 0
    while i < len(x):
        if flagged[i] and i >= BASELINE_WEEKS:
            j = i
            while j < len(x) and flagged[j]:
                j += 1
            baseline = float(np.median(x[i - BASELINE_WEEKS : i]))
            if baseline > 0:
                patterns.append((7 * (j - i), float(np.median(x[i:j])) / baseline - 1.0))
            i = j
        else:
            i += 1
    return patterns


def _elevated(value: float, baseline: float, rel: float) -> bool:
    """기준 수준보다 비율 기준(rel)에 개수 잡음 여유까지 더해 넘었는가."""
    return value > baseline * (1 + rel) + EPISODE_NOISE_K * float(np.sqrt(baseline))


def _outside(value: float, values: Sequence[float]) -> bool:
    low, high = min(values) * (1 - PROMO_PATTERN_TOLERANCE), max(values) * (1 + PROMO_PATTERN_TOLERANCE)
    return not (low <= value <= high)


def find_episodes(weekly: pd.DataFrame) -> list[Episode]:
    """기록 없는 구간에서 시작하는 상승 구간을 찾아 분류한다."""
    x = weekly["quantity"].to_numpy()
    unrecorded = weekly["unrecorded"].to_numpy()
    recorded = weekly["recorded_promo"].to_numpy()
    starts = weekly["week_start"].to_list()
    n = len(x)
    patterns = recorded_patterns(weekly)
    episodes: list[Episode] = []

    def week_date(index: int) -> pd.Timestamp:
        return starts[index] if index < n else starts[-1] + pd.DateOffset(days=7)

    i = BASELINE_WEEKS
    while i < n:
        baseline = float(np.median(x[i - BASELINE_WEEKS : i]))
        if not (baseline > 0 and _elevated(x[i], baseline, EPISODE_UPLIFT_MIN) and unrecorded[i] and not recorded[i]):
            i += 1
            continue
        j = i
        while j < n and unrecorded[j] and not recorded[j] and _elevated(x[j], baseline, PROMO_RETURN_BAND):
            j += 1
        ended = j < n and unrecorded[j] and not recorded[j]
        days = 7 * (j - i)
        run_level = float(np.median(x[i:j]))
        post = x[j : j + POST_WEEKS] if ended else np.array([])
        post_level = float(np.median(post)) if len(post) >= MIN_POST_WEEKS else None
        episode = Episode(
            start=starts[i],
            end=week_date(j),
            days=days,
            baseline=baseline,
            run_level=run_level,
            ended=ended,
            post_level=post_level,
            kind="ambiguous",
            reason="",
        )
        _classify(episode, patterns)
        episodes.append(episode)
        i = max(j, i + 1)
    return episodes


def _classify(ep: Episode, patterns: list[tuple[int, float]]) -> None:
    if ep.days <= SPIKE_MAX_DAYS:
        ep.kind, ep.reason = "spike", f"{ep.days}일 튄 구간 — 오염으로 정제"
        return
    if ep.days < EPISODE_MIN_DAYS:
        _ambiguous(ep, f"{ep.days}일 오른 구간 — 며칠 튄 날({SPIKE_MAX_DAYS}일 이하)도 한 달 이상({EPISODE_MIN_DAYS}일 이상)도 아님")
        return
    if not ep.ended:
        if ep.days >= SUSTAIN_DAYS:
            ep.kind, ep.reason = "sustained", f"{ep.days}일 동안 오른 채 유지 — 수요 증가로 보고 그대로 사용"
        else:
            _ambiguous(ep, f"{ep.days}일 올랐고 데이터가 끝나(또는 기록이 시작돼) 복귀 여부를 알 수 없음")
        return
    if ep.post_level is None:
        _ambiguous(ep, "상승이 끝난 뒤 관측이 부족해 수준을 알 수 없음")
        return
    after = ep.post_level / ep.baseline - 1.0
    if after >= PROMO_SUSTAIN_BAND:
        ep.kind, ep.reason = "sustained", f"끝난 뒤 수준이 상승 전보다 {after:+.0%} — 수요 증가로 보고 그대로 사용"
    elif after > PROMO_RETURN_BAND:
        _ambiguous(ep, f"끝난 뒤 수준이 상승 전보다 {after:+.0%} — 복귀({PROMO_RETURN_BAND:+.0%} 이하)도 유지({PROMO_SUSTAIN_BAND:+.0%} 이상)도 아님")
    else:
        ep.kind = "promotion"
        ep.use_from = ep.end.date()
        ep.reason = f"{ep.days}일 올랐다가 상승 전 수준으로 복귀({after:+.0%}) — 표시 없는 프로모션, {ep.end.date()}부터 사용"
        if patterns:
            uplift = ep.run_level / ep.baseline - 1.0
            durations = [d for d, _ in patterns]
            uplifts = [u for _, u in patterns if u > 0]
            if _outside(ep.days, durations) or (uplifts and _outside(uplift, uplifts)):
                ep.use_from = None
                _ambiguous(ep, "기록 있는 프로모션의 기간·증가폭 범위와 달라 같은 종류로 보기 어려움")
        else:
            ep.notes.append("기록 있는 프로모션이 없어 패턴 비교를 하지 못함")


def _ambiguous(ep: Episode, reason: str) -> None:
    ep.kind, ep.ambiguous, ep.reason = "ambiguous", True, reason
    ep.use_from = None
