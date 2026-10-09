"""forecast agent의 "데이터 수집과 소스 판단" 단계 (규칙 기반).

AGENT_NODE_LIST.md의 이 단계를 구현한다. 한 인스턴스((회사, item))의 입력
(`InstanceInputs`)에서 기본 데이터(`orders` + `same_item`)를 항상 쓰고, 각 소스의 상태를
확인해 다음을 기록한다.

- **오염** → 표준 정제(`cleaning`): 주문은 월별, POS는 일별로 정제한다.
- **프로모션 기록 없음 구간**: 며칠 튄 날은 정제, 한 달 이상 올랐다가 돌아온 구간은 그 구간이
  끝난 뒤부터 쓰고(`use_from`), 오른 채 유지된 구간은 수요 증가로 그대로 쓴다. 구분이 안 되면
  "애매함"으로 표시하고 데이터는 버리지 않는다(`promotion_episodes.py`).
- **무관**(`irrelevant`): 보강 후보 중 기본 데이터와 관련 없는 것은 `excluded_sources`로 보낸다.
- **시점 무관**(`outdated`): 첫 주문이 평소 수요와 크게 다르면 첫 주문 다음 달부터 쓴다(`use_from`).
- **부족**: 이력이 `MIN_HISTORY_MONTHS`개월 미만이면 그 상황에 존재하는 데이터(같은 item의 POS,
  비슷한 제품의 POS, 상품군 POS, 시장 데이터)로 이력을 앞쪽으로 늘린다. 보강은 가정마다 독립이고, 월마다
  우선순위가 가장 높은 데이터를 쓴다. 우선순위는 (1) 그 가정의 원인(driver)과 전제가 근거로 삼는 소스, (2) 같은
  item의 다른 소스(`same_item`), (3) 상위 단위 데이터(`similar_item`·`category`)다. 같은 우선순위의 후보는 기본
  데이터와 겹치는 기간으로 우리 주문 수준에 맞추고(비율), 맞춘 오차가 작은 후보에 더 큰 가중을 주어 섞는다(오차
  제곱의 역수 가중). 보강할 데이터도 없으면 사람 escalation이 필요하다고 표시한다.
- 자기 데이터가 쌓여 `MIN_HISTORY_MONTHS`개월 이상이면 보강은 자연히 빠진다.
- **사용할 주문이 없음**: 인스턴스에 완전한 달의 주문이 없거나 `use_from`을 적용한 뒤 남는 주문이 없으면 오류를
  내지 않고 `unusable_reason`을 채운 결과를 돌려준다. 이후 단계는 이 결과를 받으면 계산하지 않고 요청량을 만들 수
  없는 경우(`no_computable_assumption`)로 이어진다.

**필수 근거**: "가정 정의"가 선언한 근거 데이터를 `required_evidence`
(`(kind, item_scope)` 쌍 목록, 기본은 빈 목록)로 받아 근거마다 수집한다. 수집할 수 없으면 오류를
내지 않고 "수집 불가"(`evidence_status`)로 돌려주며, "가정별 요청량 예측값 계산"이 그 근거에 기대는
원인이 있는 가정을 제외한다. 수집한 근거는 `data_sources`에 기록돼 가정의 `evidence`가 스냅샷 안에 있는지
확인할 수 있다. 기록 없는 상승 구간을 같은 시기 시장 흐름과 비교하는 판단은 이번 범위가 아니다.

**재실행**: 데이터 수집부터 다시 도는 의심되는 원인은 `SourceAdjustments`로 들어온다. 소스를 제외하고
(`exclude`, 이유는 `irrelevant` 또는 `contaminated`), 소스를 어느 날짜부터 쓰게 하고(`use_from`), 이력이
기준을 채웠어도 대상 가정만 보강을 시도하게 한다(`force_supplement`). 같은 입력과 같은 조정이면 같은 결과다.

수치 기준은 모두 `judgment_thresholds.py`에 있다.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

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
EvidenceStatus = Literal["collected", "unavailable"]  # 필수 근거의 수집 상태("수집 불가" = unavailable)
MIN_FIT_ERROR = 0.05  # 보강 가중에서 오차가 이보다 작아도 이 값으로 본다(0으로 나누기 방지)


@dataclass
class SourceAdjustments:
    """의심되는 원인이 데이터 수집에 거는 조정. State의 `excluded_sources`와 `data_sources[].use_from`에 남는다."""

    exclude: dict[SourceKey, Literal["irrelevant", "contaminated"]] = field(default_factory=dict)
    use_from: dict[SourceKey, date] = field(default_factory=dict)
    # 이력이 `MIN_HISTORY_MONTHS` 이상이어도 값이 있는 달까지 끝까지 보강하는 가정(`insufficient`의 대상 가정)
    force_supplement: set[str] = field(default_factory=set)

    def copy(self) -> "SourceAdjustments":
        return SourceAdjustments(dict(self.exclude), dict(self.use_from), set(self.force_supplement))


@dataclass
class DataCollectionResult:
    data_sources: list[DataSource]
    excluded_sources: list[ExcludedSource]
    cleaning: Cleaning
    training_series: pd.Series  # 월별, 기본 데이터(정제·use_from 적용) + 필요하면 앞쪽 보강
    observed_start: pd.Timestamp | None  # 보강 없이 실제 주문이 있는 첫 달. 사용할 주문이 없으면 None
    judgments: list[StructuredJudgment] = field(default_factory=list)
    needs_human: bool = False
    human_reason: str | None = None
    # 필수 근거별 수집 상태와, 수집된 근거의 월별 시리즈(market은 그룹별 지수 DataFrame)
    evidence_status: dict[SourceKey, EvidenceStatus] = field(default_factory=dict)
    evidence_series: dict[SourceKey, pd.Series | pd.DataFrame] = field(default_factory=dict)
    unusable_reason: str | None = None  # 사용할 주문이 없어 계산할 수 없을 때의 이유
    # 가정마다 독립으로 보강한 학습 시리즈(가정 ID → 시리즈). `training_series`는 원인이 없는 가정 기준이다
    training_by_assumption: dict[str, pd.Series] = field(default_factory=dict)
    backcast_sources: dict[str, list[SourceKey]] = field(default_factory=dict)  # 가정 ID → 그 가정의 보강에 값을 낸 소스

    def training_for(self, assumption_id: str) -> pd.Series:
        return self.training_by_assumption.get(assumption_id, self.training_series)

    @property
    def n_observed_months(self) -> int:
        if self.observed_start is None:
            return 0
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
    """월별 주문량(주문 없는 달은 0), 기록된 프로모션 주문이 있는 달, 첫 주문이 있는 달.

    완전한 달에 해당하는 주문이 없으면 빈 시리즈를 반환한다.
    """
    if orders.empty:
        return pd.Series(dtype=float), pd.Series(dtype=bool), None
    frame = orders.assign(month=orders["order_date"].dt.to_period("M").dt.to_timestamp())
    frame = frame[frame["month"] <= last_month]
    if frame.empty:
        return pd.Series(dtype=float), pd.Series(dtype=bool), None
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
        issue="outdated" if ep.use_from else None,
        notes=ep.notes,
    )


def _refs_for(inputs: InstanceInputs, key: SourceKey) -> list[str]:
    """소스가 가리키는 item·상품군·시장 그룹. 제외한 소스를 기록할 때 쓴다."""
    if key == ("pos", "similar_item"):
        return [str(i) for i in inputs.pos_similar["item_id"].unique()] if len(inputs.pos_similar) else []
    if key == ("pos", "category"):
        return [inputs.family]
    if key == ("market", "category"):
        return list(FAMILY_MAPPING[inputs.family].market_groups)
    return [inputs.item_id]


def _supplement_candidates(
    inputs: InstanceInputs, last_month: pd.Timestamp, promo_use_from: pd.Timestamp | None,
    adjustments: SourceAdjustments | None = None,
) -> tuple[dict[SourceKey, tuple[list[str], pd.Series]], int]:
    """보강 후보 월별 시리즈. 반환: {(kind, item_scope): (refs, 시리즈)}, 후보 정제 점 수.

    `adjustments.use_from`이 있는 소스는 그 날짜부터의 값만 쓴다.
    """
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

    for key, day in (adjustments.use_from if adjustments is not None else {}).items():
        if key in candidates:
            refs, series = candidates[key]
            candidates[key] = (refs, series[series.index >= _ceil_month(day)])
    return candidates, cleaned_points


Fitted = dict[SourceKey, tuple[pd.Series, float]]  # 우리 주문 수준에 맞춘 후보 시리즈와 맞춘 오차


def _fit_candidates(
    base: pd.Series, candidates: dict, judgments: list[StructuredJudgment]
) -> tuple[Fitted, list[SourceKey]]:
    """후보를 기본 데이터 수준에 맞춘다. 반환: 쓸 수 있는 후보(맞춘 시리즈, 오차), 관련 없어 제외한 후보."""
    excluded: list[SourceKey] = []
    fitted: Fitted = {}
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
                _judgment("irrelevant_source_excluded", f"{label}: 겹치는 {len(overlap)}개월의 상관이 {corr:.2f}로 기준({MIN_RELEVANCE_CORR}) 미만 — 관련 없는 데이터로 제외", source=label, issue="irrelevant", corr=None if np.isnan(corr) else round(corr, 3))
            )
            continue
        if c.sum() <= 0 or b.sum() <= 0:
            judgments.append(_judgment("supplement_unusable", f"{label}: 겹치는 기간의 합이 0이라 비율을 정할 수 없음", source=label))
            continue
        ratio = float(b.sum() / c.sum())
        error = float(np.mean(np.abs(b - ratio * c)) / b.mean())
        fitted[key] = (series * ratio, error)
        judgments.append(
            _judgment("supplement_used", f"{label}: 상관 {corr:.2f}, 우리 수준에 맞춘 비율 {ratio:.3g}, 맞춘 오차 {error:.0%}", source=label, corr=round(corr, 3), ratio=ratio, fit_error=round(error, 4))
        )
    return fitted, excluded


def _source_tier(key: SourceKey, own_sources: frozenset[SourceKey]) -> int:
    """보강 우선순위: 1 = 그 가정의 원인·전제가 근거로 삼는 소스, 2 = 같은 item의 다른 소스, 3 = 상위 단위 데이터."""
    if key in own_sources:
        return 1
    return 2 if key[1] == "same_item" else 3


MAX_BACKCAST_MONTHS = 1200  # 끝까지 보강할 때 반복이 끝나지 않는 일을 막는 안전 상한(100년)


def _fill_backcast(
    base: pd.Series, fitted: Fitted, own_sources: frozenset[SourceKey], months: int | None
) -> tuple[pd.Series, list[SourceKey]]:
    """한 가정의 base 앞쪽 이력을 만든다. 월마다 우선순위가 가장 높은 티어에서 값이 있는 후보를 섞어 쓴다.

    `months`가 None이면 후보에 값이 있는 달까지 끝까지 이어 붙인다. 반환: (보강 시리즈, 값을 낸 후보).
    """
    tiers = sorted({_source_tier(k, own_sources) for k in fitted})
    values: dict[pd.Timestamp, float] = {}
    contributed: list[SourceKey] = []
    month = base.index[0] - pd.DateOffset(months=1)
    limit = MAX_BACKCAST_MONTHS if months is None else months
    for _ in range(limit):
        value = None
        for tier in tiers:
            num = den = 0.0
            keys: list[SourceKey] = []
            for key, (scaled, error) in fitted.items():
                if _source_tier(key, own_sources) != tier or month not in scaled.index:
                    continue
                weight = 1.0 / max(error, MIN_FIT_ERROR) ** 2
                num += weight * float(scaled.loc[month])
                den += weight
                keys.append(key)
            if den > 0:
                value = num / den
                contributed.extend(k for k in keys if k not in contributed)
                break
        if value is None:
            break
        values[month] = value
        month = month - pd.DateOffset(months=1)
    return pd.Series(values, dtype=float).sort_index(), contributed


def _collect_required_evidence(
    inputs: InstanceInputs,
    required: list[SourceKey],
    last_month: pd.Timestamp,
    promo_use_from: pd.Timestamp | None,
    base: pd.Series,
    data_sources: list[DataSource],
    judgments: list[StructuredJudgment],
    adjustments: SourceAdjustments | None = None,
) -> tuple[dict[SourceKey, EvidenceStatus], dict[SourceKey, pd.Series | pd.DataFrame]]:
    """필수 근거를 수집한다. 수집한 근거는 `data_sources`에 없으면 추가한다. 반환: 상태, 시리즈.

    근거로 쓰는 POS도 일별로 정제한 월별 값이다(정제 점 수는 `cleaning`에 이미 센 것과 겹쳐 다시 세지 않는다).
    """
    status: dict[SourceKey, EvidenceStatus] = {}
    series: dict[SourceKey, pd.Series | pd.DataFrame] = {}
    if not required:
        return status, series
    candidates, _ = _supplement_candidates(inputs, last_month, promo_use_from, adjustments)
    for key in dict.fromkeys(required):
        label = f"{key[0]}/{key[1]}"
        if adjustments is not None and key in adjustments.exclude:
            status[key] = "unavailable"
            judgments.append(_judgment("evidence_unavailable", f"{label}: 의심되는 원인({adjustments.exclude[key]})로 제외한 소스라 수집하지 않음", source=label))
            continue
        if key == ("orders", "same_item"):
            status[key], series[key] = "collected", base
            continue
        if key == ("market", "category"):
            frame = inputs.market_group_index
            if frame is None and inputs.market_index is not None:
                frame = inputs.market_index.to_frame()
            if frame is None or frame.empty:
                status[key] = "unavailable"
                judgments.append(_judgment("evidence_unavailable", f"{label}: 시장 데이터가 없어 수집 불가", source=label))
                continue
            frame = frame[frame.index <= last_month]
            if adjustments is not None and key in adjustments.use_from:
                frame = frame[frame.index >= _ceil_month(adjustments.use_from[key])]
            status[key], series[key] = "collected", frame
            refs = list(frame.columns)
        elif key in candidates:
            refs, found = candidates[key]
            status[key], series[key] = "collected", found
        else:
            status[key] = "unavailable"
            judgments.append(_judgment("evidence_unavailable", f"{label}: 이 데이터를 수집할 수 없음(없거나 제공되지 않는 조합)", source=label))
            continue
        if not any((s.kind, s.item_scope) == key for s in data_sources):
            data_sources.append(DataSource(kind=key[0], item_scope=key[1], refs=[str(r) for r in refs]))
        judgments.append(_judgment("evidence_collected", f"{label}: 가정의 필수 근거로 수집", source=label))
    return status, series


def _unusable_result(
    inputs: InstanceInputs, reason: str, judgments: list[StructuredJudgment],
    data_sources: list[DataSource] | None = None, cleaned_points: int = 0,
) -> DataCollectionResult:
    """사용할 주문이 없어 계산할 수 없다는 결과. 오류로 멈추지 않고 이후 단계가 요청량을 만들 수 없는 경우로 처리한다."""
    judgments.append(_judgment("no_usable_orders", reason))
    log(ROLE_TAG, "collect_instance_data", company_id=inputs.company_id, item_id=inputs.item_id, unusable=reason)
    return DataCollectionResult(
        data_sources=data_sources or [],
        excluded_sources=[],
        cleaning=Cleaning(applied=cleaned_points > 0, count=cleaned_points),
        training_series=pd.Series(dtype=float, index=pd.DatetimeIndex([])),
        observed_start=None,
        judgments=judgments,
        unusable_reason=reason,
    )


def collect_instance_data(
    inputs: InstanceInputs, required_evidence: list[SourceKey] | None = None,
    adjustments: SourceAdjustments | None = None,
    assumption_sources: dict[str, frozenset[SourceKey]] | None = None,
) -> DataCollectionResult:
    """데이터 수집과 소스 판단. 상태(State)는 건드리지 않고 결과를 반환한다.

    `required_evidence`는 가정 정의가 선언한 근거 목록이다. 근거마다 수집하고, 수집할 수
    없으면 오류 대신 `evidence_status`에 "unavailable"로 돌려준다. `adjustments`는 재실행이
    거는 소스 조정이다. `assumption_sources`는 가정 ID별로 그 가정의 원인·전제가 근거로 삼는 소스이며, 이 소스를
    가장 먼저 써서 가정마다 따로 보강한다(`training_by_assumption`).
    """
    adjustments = adjustments if adjustments is not None else SourceAdjustments()
    judgments: list[StructuredJudgment] = []
    last_month = last_complete_month(inputs.data_end)
    cleaned_points = 0

    orders_monthly, promo_months, first_order_month = _monthly_orders(inputs.orders, last_month)
    if orders_monthly.empty:
        return _unusable_result(inputs, "완전한 달에 해당하는 주문이 없음", judgments)

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
                        "outdated_first_order_use_from",
                        f"첫 주문이 있는 {first_order_month:%Y-%m}의 주문량이 이후 {len(following)}개월 중앙값의 {ratio:.1f}배 — 평소 수요와 달라 {first_use_from:%Y-%m}부터 사용",
                        source="orders/same_item", issue="outdated", ratio=round(ratio, 2), use_from=str(first_use_from.date()),
                    )
                )

    adjusted_from = adjustments.use_from.get(("orders", "same_item"))
    if adjusted_from is not None:
        judgments.append(
            _judgment("outdated_use_from_applied", f"의심되는 원인으로 주문을 {adjusted_from}부터 사용", source="orders/same_item", issue="outdated", use_from=str(adjusted_from))
        )
    orders_cut = [
        d for d in (promo_use_from, first_use_from, pd.Timestamp(adjusted_from) if adjusted_from else None) if d is not None
    ]
    orders_use_from = max(orders_cut) if orders_cut else None
    base = orders_monthly
    if orders_use_from is not None:
        base = orders_monthly[orders_monthly.index >= _ceil_month(orders_use_from)]
    if base.empty:
        orders_source = DataSource(
            kind="orders", item_scope="same_item", refs=[inputs.item_id],
            use_from=orders_use_from.date() if orders_use_from is not None else None,
        )
        return _unusable_result(
            inputs, "use_from 적용 후 남는 주문 데이터가 없음", judgments, [orders_source], cleaned_points
        )

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
    training_by_assumption = {assumption_id: base for assumption_id in assumption_sources or {}}
    backcast_sources: dict[str, list[SourceKey]] = {assumption_id: [] for assumption_id in assumption_sources or {}}
    needs_human, human_reason = False, None

    force = adjustments.force_supplement
    if len(base) >= MIN_HISTORY_MONTHS and not force:
        judgments.append(_judgment("history_sufficient", f"사용 가능한 이력 {len(base)}개월(기준 {MIN_HISTORY_MONTHS}개월 이상) — 기본 데이터만 사용", months=len(base)))
    else:
        candidates, extra_cleaned = _supplement_candidates(inputs, last_month, promo_use_from, adjustments)
        cleaned_points += extra_cleaned
        candidates = {k: v for k, v in candidates.items() if k not in adjustments.exclude}
        fitted, excluded = _fit_candidates(base, candidates, judgments)
        for key in excluded:
            refs, _ = candidates[key]
            excluded_sources.append(ExcludedSource(kind=key[0], item_scope=key[1], refs=refs))
        short = len(base) < MIN_HISTORY_MONTHS
        months = max(HISTORY_TARGET_MONTHS - len(base), 0)
        backcast, used = _fill_backcast(base, fitted, frozenset(), months) if short else (pd.Series(dtype=float), [])
        fills = {}
        for assumption_id, own in (assumption_sources or {}).items():
            if assumption_id in force:  # 재실행의 대상 가정은 값이 있는 달까지 끝까지 보강한다
                fills[assumption_id] = _fill_backcast(base, fitted, own, None)
            elif short:
                fills[assumption_id] = _fill_backcast(base, fitted, own, months)
        for assumption_id, (fill, contributed) in fills.items():
            backcast_sources[assumption_id] = contributed
            if len(fill):
                training_by_assumption[assumption_id] = pd.concat([fill, base])
        used = [k for k in fitted if k in set(used) | {u for _, keys in fills.values() for u in keys}]
        if any(len(fills[assumption_id][0]) == 0 for assumption_id in force if assumption_id in fills):
            judgments.append(_judgment("no_additional_supplement", f"의심되는 원인으로 보강을 시도했으나 이력 {len(base)}개월 앞쪽에 더 얹을 보강 데이터가 없는 가정이 있음", months=len(base)))
        if short and len(backcast) == 0:
            needs_human = True
            human_reason = f"이력 {len(base)}개월로 부족한데 보강에 쓸 수 있는 데이터가 없음"
            judgments.append(_judgment("escalate_no_supplement", human_reason, months=len(base)))
        elif short:
            training = pd.concat([backcast, base])
            judgments.append(
                _judgment("history_supplemented", f"이력 {len(base)}개월로 부족(기준 {MIN_HISTORY_MONTHS}개월)해 {len(used)}개 소스로 앞쪽 {len(backcast)}개월을 보강", observed_months=len(base), backcast_months=len(backcast), sources=[f"{k[0]}/{k[1]}" for k in used])
            )
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

    evidence_status, evidence_series = _collect_required_evidence(
        inputs, required_evidence or [], last_month, promo_use_from, base, data_sources, judgments, adjustments
    )
    for key, reason in adjustments.exclude.items():  # 의심되는 원인으로 제외한 소스는 항상 남긴다
        excluded_sources = [e for e in excluded_sources if (e.kind, e.item_scope) != key]
        excluded_sources.append(ExcludedSource(kind=key[0], item_scope=key[1], refs=_refs_for(inputs, key), reason=reason))
    for key, day in adjustments.use_from.items():  # 소스를 쓰기 시작하는 날짜를 남긴다
        if key == ("orders", "same_item"):
            continue
        for index, source in enumerate(data_sources):
            if (source.kind, source.item_scope) == key:
                data_sources[index] = source.model_copy(update={"use_from": day})

    log(ROLE_TAG, "collect_instance_data", company_id=inputs.company_id, item_id=inputs.item_id,
        observed_months=len(base), training_months=len(training), cleaned=cleaned_points,
        sources=len(data_sources), excluded=len(excluded_sources), needs_human=needs_human)
    return DataCollectionResult(
        data_sources=data_sources,
        excluded_sources=excluded_sources,
        cleaning=Cleaning(applied=cleaned_points > 0, count=cleaned_points),
        training_series=training,
        training_by_assumption=training_by_assumption,
        backcast_sources=backcast_sources,
        observed_start=base.index[0],
        judgments=judgments,
        needs_human=needs_human,
        human_reason=human_reason,
        evidence_status=evidence_status,
        evidence_series=evidence_series,
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
