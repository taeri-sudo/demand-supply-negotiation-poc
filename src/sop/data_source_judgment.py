"""forecast agent 내부 2단계 — 데이터 수집과 데이터 소스 판단 (규칙 기반).

AGENT_NODE_LIST.md forecast agent 2단계를 구현한다. 한 인스턴스((회사, item))의 입력
(`InstanceInputs`)에서 기본 데이터(`orders` + `same_item`)를 항상 쓰고, 각 소스의 상태를
확인해 다음을 기록한다.

- **오염** → 표준 정제(`cleaning`): 주문은 월별, POS는 일별로 정제한다.
- **프로모션 기록 없음 구간**: 며칠 튄 날은 정제, 한 달 이상 올랐다가 돌아온 구간은 그 구간이
  끝난 뒤부터 쓰고(`use_from`), 오른 채 유지된 구간은 수요 증가로 그대로 쓴다. 구분이 안 되면
  "애매함"으로 표시하고 데이터는 버리지 않는다(`promotion_episodes.py`).
- **무관**: 첫 주문이 평소 수요와 크게 다르면 첫 주문 다음 달부터 쓴다(`use_from`). 보강 후보 중
  기본 데이터와 관련 없는 것은 `excluded_sources`로 보낸다.
- **부족**: 이력이 `MIN_HISTORY_MONTHS`개월 미만이면 그 상황에 존재하는 데이터(같은 item의 POS,
  비슷한 제품의 POS, 상품군 POS, 시장 데이터)로 이력을 앞쪽으로 늘린다(고정 우선순위 없음).
  후보마다 기본 데이터와 겹치는 기간으로 우리 주문 수준에 맞추고(비율), 맞춘 오차가 작은
  후보에 더 큰 가중을 주어 섞는다(오차 제곱의 역수 가중). 보강할 데이터도 없으면 사람
  escalation이 필요하다고 표시한다.
- 자기 데이터가 쌓여 `MIN_HISTORY_MONTHS`개월 이상이면 보강은 자연히 빠진다.

시나리오가 요구하는 근거 데이터를 입력으로 받는 부분은 시나리오 정의(M2 4단계)와 함께
정해진다. 시장 변화율이 필요한 판단(기록 없는 상승 구간을 같은 시기 시장 흐름과 비교)은
변화율 기준이 정해질 때까지 만들지 않았다.

수치 기준은 모두 `judgment_thresholds.py`에 있다.
"""

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .data_cleaning import CleaningResult, clean_series
from .external_data import InstanceInputs
from .favorita_mapping import FAMILY_MAPPING
from .judgment import StructuredJudgment
from .judgment_thresholds import (
    CLEANING_K_DAILY,
    CLEANING_K_MONTHLY,
    DAILY_SEASON_PERIOD,
    FALLBACK_MEDIAN_WINDOW_DAILY,
    FALLBACK_MEDIAN_WINDOW_MONTHLY,
    FIRST_ORDER_FOLLOWING_MONTHS,
    FIRST_ORDER_RATIO,
    HISTORY_TARGET_MONTHS,
    MIN_HISTORY_MONTHS,
    MIN_OVERLAP_MONTHS,
    MIN_RELEVANCE_CORR,
    MONTHLY_SEASON_PERIOD,
)
from .logging_utils import log
from .promotion_episodes import Episode, deseasonalize_weekly, find_episodes, weekly_table
from .state import Cleaning, DataKind, DataSource, ExcludedSource, ForecastRecord, ItemScope

ROLE_TAG = "forecast"
SourceKey = tuple[DataKind, ItemScope]  # (kind, item_scope) — 후보 소스를 가리키는 키
MIN_FIT_ERROR = 0.05  # 보강 가중에서 오차가 이보다 작아도 이 값으로 본다(0으로 나누기 방지)


@dataclass
class DataCollectionResult:
    data_sources: list[DataSource]
    excluded_sources: list[ExcludedSource]
    cleaning: Cleaning
    training_series: pd.Series  # 월별, 기본 데이터(정제·use_from 적용) + 필요하면 앞쪽 보강
    observed_start: pd.Timestamp  # 보강 없이 실제 주문이 있는 첫 달
    judgments: list[StructuredJudgment] = field(default_factory=list)
    needs_human: bool = False
    human_reason: str | None = None

    @property
    def n_observed_months(self) -> int:
        return int((self.training_series.index >= self.observed_start).sum())


def last_complete_month(data_end: pd.Timestamp) -> pd.Timestamp:
    """데이터 끝 날짜 기준으로 마지막 완전한 달의 첫날."""
    end = pd.Timestamp(data_end).normalize()
    period = end.to_period("M")
    if end != end + pd.offsets.MonthEnd(0):
        period = period - 1
    return period.to_timestamp()


def _ceil_month(day: date | pd.Timestamp) -> pd.Timestamp:
    """`day` 이후 처음 오는 달의 첫날(day가 1일이면 그 달)."""
    ts = pd.Timestamp(day)
    return ts.to_period("M").to_timestamp() if ts.day == 1 else (ts.to_period("M") + 1).to_timestamp()


def _monthly_orders(
    orders: pd.DataFrame, last_month: pd.Timestamp
) -> tuple[pd.Series, pd.Series, pd.Timestamp | None]:
    """월별 주문량(주문 없는 달은 0), 기록된 프로모션 주문이 있는 달, 첫 주문이 있는 달."""
    frame = orders.assign(month=orders["order_date"].dt.to_period("M").dt.to_timestamp())
    frame = frame[frame["month"] <= last_month]
    if frame.empty:
        raise ValueError("완전한 달에 해당하는 주문이 없음")
    index = pd.date_range(frame["month"].min(), last_month, freq="MS")
    quantity = frame.groupby("month")["quantity"].sum().reindex(index, fill_value=0.0)
    promo = frame.groupby("month")["promotion"].apply(lambda s: bool(s.eq(True).any()))
    promo = promo.reindex(index, fill_value=False)
    first = frame.loc[frame["is_first_order"], "month"]
    return quantity, promo, (first.iloc[0] if len(first) else None)


def _prepare_daily(
    pos: pd.DataFrame, data_end: pd.Timestamp
) -> tuple[pd.Series, pd.Series, pd.Timestamp | None, CleaningResult]:
    """일별 POS를 결측일 0으로 채우고 정제한다. (정제본, 프로모션 시리즈, 기록 시작일, 정제 결과)."""
    daily = pos.groupby("date")["quantity"].sum()
    index = pd.date_range(daily.index.min(), pd.Timestamp(data_end).normalize(), freq="D")
    quantity = daily.reindex(index, fill_value=0.0)
    promotion = pos.groupby("date")["promotion"].first().reindex(index)
    recorded = promotion.dropna()
    record_start = recorded.index.min() if len(recorded) else None
    protected = promotion.eq(True).fillna(False)
    result = clean_series(
        quantity, DAILY_SEASON_PERIOD, CLEANING_K_DAILY, FALLBACK_MEDIAN_WINDOW_DAILY, protected
    )
    return result.cleaned, promotion, record_start, result


def _to_monthly(daily: pd.Series, last_month: pd.Timestamp) -> pd.Series:
    month = pd.DatetimeIndex(daily.index).to_period("M").to_timestamp()
    monthly = daily.groupby(month).sum()
    index = pd.date_range(monthly.index.min(), last_month, freq="MS")
    return monthly[monthly.index <= last_month].reindex(index, fill_value=0.0)


def _judgment(
    decision: str, reasoning: str, ambiguous: bool = False, ambiguity_reason: str | None = None, **values
) -> StructuredJudgment:
    return StructuredJudgment(
        judgment={"decision": decision, **values},
        reasoning=reasoning,
        ambiguous=ambiguous,
        ambiguity_reason=ambiguity_reason,
    )


def _episode_judgment(ep: Episode) -> StructuredJudgment:
    return _judgment(
        "promotion_gap_episode",
        ep.reason,
        ambiguous=ep.ambiguous,
        ambiguity_reason=ep.reason if ep.ambiguous else None,
        kind=ep.kind,
        start=str(ep.start.date()),
        end=str(ep.end.date()),
        days=ep.days,
        use_from=str(ep.use_from) if ep.use_from else None,
        notes=ep.notes,
    )


def _supplement_candidates(
    inputs: InstanceInputs, last_month: pd.Timestamp, promo_use_from: pd.Timestamp | None
) -> tuple[dict[SourceKey, tuple[list[str], pd.Series]], int]:
    """보강 후보 월별 시리즈. 반환: {(kind, item_scope): (refs, 시리즈)}, 후보 정제 점 수."""
    candidates: dict[SourceKey, tuple[list[str], pd.Series]] = {}
    cleaned_points = 0

    if len(inputs.pos_same):
        daily, _, _, result = _prepare_daily(inputs.pos_same, inputs.data_end)
        cleaned_points += result.count
        series = _to_monthly(daily, last_month)
        if promo_use_from is not None:
            series = series[series.index >= promo_use_from]
        if len(series):
            candidates[("pos", "same_item")] = ([inputs.item_id], series)

    if len(inputs.pos_similar):
        parts, refs = [], []
        for item_id, group in inputs.pos_similar.groupby("item_id"):
            daily, _, _, result = _prepare_daily(group, inputs.data_end)
            cleaned_points += result.count
            parts.append(_to_monthly(daily, last_month))
            refs.append(str(item_id))
        index = pd.date_range(min(p.index.min() for p in parts), last_month, freq="MS")
        mean = pd.concat([p.reindex(index, fill_value=0.0) for p in parts], axis=1).mean(axis=1)
        candidates[("pos", "similar_item")] = (refs, mean)

    if len(inputs.pos_category):
        category = inputs.pos_category.set_index("month")["quantity"].sort_index()
        candidates[("pos", "category")] = ([inputs.family], category[category.index <= last_month])

    if inputs.market_index is not None and len(inputs.market_index):
        market = inputs.market_index.sort_index()
        groups = list(FAMILY_MAPPING[inputs.family].market_groups)
        candidates[("market", "category")] = (groups, market[market.index <= last_month])

    return candidates, cleaned_points


def _backcast(
    base: pd.Series, candidates: dict, judgments: list[StructuredJudgment]
) -> tuple[pd.Series, list[SourceKey], list[SourceKey]]:
    """후보를 기본 데이터 수준에 맞춰 섞어 base 앞쪽 이력을 만든다. (보강 시리즈, 사용 후보, 제외 후보)."""
    used: list[SourceKey] = []
    excluded: list[SourceKey] = []
    fitted: dict[SourceKey, tuple[pd.Series, float]] = {}
    for key, (_, series) in candidates.items():
        overlap = base.index.intersection(series.index)
        label = f"{key[0]}/{key[1]}"
        if len(overlap) < MIN_OVERLAP_MONTHS:
            judgments.append(
                _judgment("supplement_unusable", f"{label}: 기본 데이터와 겹치는 달이 {len(overlap)}개로 부족해 우리 수준에 맞출 수 없음", source=label)
            )
            continue
        b, c = base.loc[overlap].to_numpy(), series.loc[overlap].to_numpy()
        corr = float(np.corrcoef(b, c)[0, 1]) if b.std() > 0 and c.std() > 0 else float("nan")
        if np.isnan(corr) or corr < MIN_RELEVANCE_CORR:
            excluded.append(key)
            judgments.append(
                _judgment("supplement_excluded", f"{label}: 겹치는 {len(overlap)}개월의 상관이 {corr:.2f}로 기준({MIN_RELEVANCE_CORR}) 미만 — 관련 없는 데이터로 제외", source=label, corr=None if np.isnan(corr) else round(corr, 3))
            )
            continue
        if c.sum() <= 0 or b.sum() <= 0:
            judgments.append(_judgment("supplement_unusable", f"{label}: 겹치는 기간의 합이 0이라 비율을 정할 수 없음", source=label))
            continue
        ratio = float(b.sum() / c.sum())
        error = float(np.mean(np.abs(b - ratio * c)) / b.mean())
        fitted[key] = (series * ratio, error)
        used.append(key)
        judgments.append(
            _judgment("supplement_used", f"{label}: 상관 {corr:.2f}, 우리 수준에 맞춘 비율 {ratio:.3g}, 맞춘 오차 {error:.0%}", source=label, corr=round(corr, 3), ratio=ratio, fit_error=round(error, 4))
        )

    if not used:
        return pd.Series(dtype=float), used, excluded

    need = HISTORY_TARGET_MONTHS - len(base)
    values: dict[pd.Timestamp, float] = {}
    month = base.index[0] - pd.DateOffset(months=1)
    for _ in range(max(need, 0)):
        num = den = 0.0
        for key in used:
            scaled, error = fitted[key]
            if month in scaled.index:
                weight = 1.0 / max(error, MIN_FIT_ERROR) ** 2
                num += weight * float(scaled.loc[month])
                den += weight
        if den == 0:
            break
        values[month] = num / den
        month = month - pd.DateOffset(months=1)
    backcast = pd.Series(values, dtype=float).sort_index()
    return backcast, used, excluded


def collect_instance_data(inputs: InstanceInputs) -> DataCollectionResult:
    """데이터 수집과 소스 판단. 상태(State)는 건드리지 않고 결과를 반환한다."""
    judgments: list[StructuredJudgment] = []
    last_month = last_complete_month(inputs.data_end)
    cleaned_points = 0

    orders_monthly, promo_months, first_order_month = _monthly_orders(inputs.orders, last_month)

    # 프로모션 기록 없음 구간: 같은 고객사·item의 일별 POS로 상승 구간을 분류한다
    promo_use_from: pd.Timestamp | None = None
    if len(inputs.pos_same):
        daily, promotion, record_start, daily_result = _prepare_daily(inputs.pos_same, inputs.data_end)
        cleaned_points += daily_result.count
        if daily_result.count:
            judgments.append(
                _judgment(
                    "cleaning", f"일별 POS에서 {daily_result.count}개 점 정제(음수 {daily_result.n_negative}, 이상치 {daily_result.n_outliers})",
                    source="pos/same_item", count=daily_result.count,
                )
            )
        weekly = deseasonalize_weekly(weekly_table(daily, promotion, inputs.data_end, record_start))
        episodes = find_episodes(weekly)
        judgments.extend(_episode_judgment(ep) for ep in episodes)
        ends = [pd.Timestamp(ep.use_from) for ep in episodes if ep.use_from]
        promo_use_from = max(ends) if ends else None

    # 첫 주문이 평소 수요와 크게 다르면 그 다음 달부터 쓴다
    first_use_from: pd.Timestamp | None = None
    if first_order_month is not None:
        following = orders_monthly.loc[
            first_order_month + pd.DateOffset(months=1) : first_order_month + pd.DateOffset(months=FIRST_ORDER_FOLLOWING_MONTHS)
        ]
        if len(following) >= 3 and following.median() > 0:
            ratio = float(orders_monthly.loc[first_order_month] / following.median())
            if ratio >= FIRST_ORDER_RATIO:
                first_use_from = first_order_month + pd.DateOffset(months=1)
                judgments.append(
                    _judgment(
                        "first_order_use_from",
                        f"첫 주문이 있는 {first_order_month:%Y-%m}의 주문량이 이후 {len(following)}개월 중앙값의 {ratio:.1f}배 — 평소 수요와 달라 {first_use_from:%Y-%m}부터 사용",
                        source="orders/same_item", ratio=round(ratio, 2), use_from=str(first_use_from.date()),
                    )
                )

    orders_cut = [d for d in (promo_use_from, first_use_from) if d is not None]
    orders_use_from = max(orders_cut) if orders_cut else None
    base = orders_monthly
    if orders_use_from is not None:
        base = orders_monthly[orders_monthly.index >= _ceil_month(orders_use_from)]
    if base.empty:
        raise ValueError("use_from 적용 후 남는 주문 데이터가 없음")

    # 주문 월별 정제(기록 있는 프로모션 달은 정상적인 수요 효과이므로 정제하지 않는다)
    order_result = clean_series(
        base, MONTHLY_SEASON_PERIOD, CLEANING_K_MONTHLY, FALLBACK_MEDIAN_WINDOW_MONTHLY, promo_months
    )
    base = order_result.cleaned
    cleaned_points += order_result.count
    if order_result.count:
        judgments.append(
            _judgment("cleaning", f"월별 주문에서 {order_result.count}개 점 정제(음수 {order_result.n_negative}, 이상치 {order_result.n_outliers})", source="orders/same_item", count=order_result.count)
        )

    data_sources = [
        DataSource(
            kind="orders",
            item_scope="same_item",
            refs=[inputs.item_id],
            use_from=orders_use_from.date() if orders_use_from is not None else None,
        )
    ]
    excluded_sources: list[ExcludedSource] = []
    training = base
    needs_human, human_reason = False, None

    if len(base) >= MIN_HISTORY_MONTHS:
        judgments.append(_judgment("history_sufficient", f"사용 가능한 이력 {len(base)}개월(기준 {MIN_HISTORY_MONTHS}개월 이상) — 기본 데이터만 사용", months=len(base)))
    else:
        candidates, extra_cleaned = _supplement_candidates(inputs, last_month, promo_use_from)
        cleaned_points += extra_cleaned
        backcast, used, excluded = _backcast(base, candidates, judgments)
        for key in excluded:
            refs, _ = candidates[key]
            excluded_sources.append(ExcludedSource(kind=key[0], item_scope=key[1], refs=refs))
        if len(backcast) == 0:
            needs_human = True
            human_reason = f"이력 {len(base)}개월로 부족한데 보강에 쓸 수 있는 데이터가 없음"
            judgments.append(_judgment("escalate_no_supplement", human_reason, months=len(base)))
        else:
            training = pd.concat([backcast, base])
            for key in used:
                refs, _ = candidates[key]
                data_sources.append(
                    DataSource(
                        kind=key[0],
                        item_scope=key[1],
                        refs=refs,
                        use_from=promo_use_from.date() if key == ("pos", "same_item") and promo_use_from is not None else None,
                    )
                )
            judgments.append(
                _judgment("history_supplemented", f"이력 {len(base)}개월로 부족(기준 {MIN_HISTORY_MONTHS}개월)해 {len(used)}개 소스로 앞쪽 {len(backcast)}개월을 보강", observed_months=len(base), backcast_months=len(backcast), sources=[f"{k[0]}/{k[1]}" for k in used])
            )

    log(ROLE_TAG, "collect_instance_data", company_id=inputs.company_id, item_id=inputs.item_id,
        observed_months=len(base), training_months=len(training), cleaned=cleaned_points,
        sources=len(data_sources), excluded=len(excluded_sources), needs_human=needs_human)
    return DataCollectionResult(
        data_sources=data_sources,
        excluded_sources=excluded_sources,
        cleaning=Cleaning(applied=cleaned_points > 0, count=cleaned_points),
        training_series=training,
        observed_start=base.index[0],
        judgments=judgments,
        needs_human=needs_human,
        human_reason=human_reason,
    )


def apply_to_record(record: ForecastRecord, result: DataCollectionResult) -> ForecastRecord:
    """수집 결과를 forecast_records의 데이터 소스 필드에 반영한 새 레코드를 반환한다."""
    return record.model_copy(
        update={
            "data_sources": result.data_sources,
            "excluded_sources": result.excluded_sources,
            "cleaning": result.cleaning,
        }
    )
