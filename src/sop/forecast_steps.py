"""forecast agent의 내부 단계 1-4(가정 정의 → 데이터 수집 → 원인 확인 → 통계기법 선택·요청량 계산)와 재실행.

`run_forecast_steps`가 한 인스턴스의 첫 실행이다. State는 건드리지 않고 결과(`ForecastStepsResult`)를 반환한다.
5단계(가정 선택)와 State 반영은 `forecast_supply_allocation.py`가 한다.

**재실행**(AGENT_NODE_LIST.md "재실행", 용어는 GRAPH_FLOW.md): `ForecastRerunHandling`이 한 번의
재실행 처리를 맡는다. `rerun(causes)`는 직전 결과와 의심되는 원인 목록(`suspected_causes`)을 입력으로 받아
재개 지점부터 이후 단계를 전부 다시 돈다. 이유가 여러 개면 그중 가장 앞 재개 지점에서 시작하고, 한 가정에 이유가
여럿이면 단계 순서대로 적용하며, 수단 없음 비교는 모든 이유를 적용한 뒤 가정마다 한 번만 한다. 같은 직전 결과와
같은 이유면 같은 결과다.

**범위**: 재실행의 모든 대응(보강, 소스 제외, 시점 적용, 기법 제외, 입력 불변 규칙에 따른 제외)은 대상 가정에만
적용하고, 대상이 아닌 가정은 어떤 대응도 받지 않는다. 대상 목록은 이유마다 `rerun`의 `targets_of()` 하나다.
- `assumption`: `assumption_id`의 가정
- `data_source`: `source`를 쓰는 가정(`_uses_source`). `source`가 없는 `insufficient`는 모든 가정
- `method_selection`: 모든 가정

| `suspected_cause` | 재개 지점 | 대응 |
|---|---|---|
| `assumption` | 가정 정의 | `drivers`가 있으면 그 원인만, null이면 모든 원인을 제외하고 다시 정의. 원인이 하나도 남지 않으면 수단 없음 |
| `data_source` + `insufficient` | 데이터 수집 | 보강 데이터를 더 얹는다(끝까지 이어 붙임) |
| `data_source` + `contaminated` | 데이터 수집 | 그 소스를 제외. `orders`는 제외할 수 없다 |
| `data_source` + `outdated` | 데이터 수집 | 그 소스를 `use_from`부터 사용 |
| `data_source` + `irrelevant` | 데이터 수집 | 그 소스를 `excluded_sources`로 이동. `orders`는 받지 않는다 |
| `method_selection` | 통계기법 선택(수집 데이터 재사용) | 직전 기법 구성을 제외하고 재선택. 남은 구성이 없으면 수단 없음 |

**수단 없음이면 그 가정을 제외한다**: `excluded_assumptions`에 의심되는 원인을 제외 이유로 기록한다. 대상 가정의
입력이 재실행해도 직전 재실행과 같으면 수단 없음이다. 입력은 가정의 원인, 가정의 학습 시리즈, 원인의 근거 시리즈,
제외한 기법이다.
가정이 모두 제외되면 이 모듈은 가정이 없는 결과를 돌려주고, `options_exhausted`와 `no_computable_assumption`의 구분은
State를 보는 `forecast_supply_allocation.py`가 한다.

`method_selection`의 기법 제외는 한 번의 재실행 처리(`ForecastRerunHandling` 객체) 동안 가정 안에서만
누적한다. 이 객체 안에서만 들고 있고 State 필드는 없다. 다른 가정이 같은 기법을 쓰는 것은 문제없다. 재실행
한 번마다 대상 가정의 입력이 바뀌거나 그 가정이 제외돼 가정이 줄므로 반드시 끝난다. 재실행 결과 비교나 반복
횟수 세기는 쓰지 않는다.
"""

import hashlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data_source_judgment import (
    DataCollectionResult,
    SourceAdjustments,
    SourceKey,
    collect_instance_data,
    last_complete_month,
)
from .external_data import InstanceInputs
from .forecast_assumption_calc import CalculatedAssumptions, select_methods_and_calculate_values
from .forecast_assumption_definition import (
    AssumptionDefinition,
    define_assumptions,
    redefine_assumptions,
    required_evidence_for,
)
from .forecast_driver_check import DriverCheck, check_drivers
from .judgment import StructuredJudgment
from .logging_utils import log
from .state import Assumption, Driver, ExcludedAssumption, ExcludedDriver, ForecastRecord, SuspectedCause, suspected_cause_reason

ROLE_TAG = "forecast"
# 의심되는 원인의 문제 대상별 재개 지점(내부 단계 번호)
_RESUME_POINT = {"assumption": 1, "data_source": 2, "method_selection": 3}


@dataclass
class StageTracker:
    """지금 실행 중인 내부 단계 이름. 실행이 예외로 끝났을 때 어느 단계였는지 로그에 남기려고 단계가 바뀔 때마다 갱신한다."""

    step: str = "start"


@dataclass
class ForecastStepsResult:
    """내부 단계 1-4의 결과. 재실행의 직전 결과이기도 하다."""

    scheduled_promotion: bool
    defined: AssumptionDefinition
    collection: DataCollectionResult
    check: DriverCheck
    calculated: CalculatedAssumptions
    adjustments: SourceAdjustments = field(default_factory=SourceAdjustments)
    dropped: list[ExcludedAssumption] = field(default_factory=list)  # 의심되는 원인으로 제외한 가정
    dropped_drivers: list[ExcludedDriver] = field(default_factory=list)  # 의심되는 원인으로 제외한 원인
    fingerprints: dict[str, str] = field(default_factory=dict)  # 가정 ID → 그 가정의 입력(원인·학습 시리즈·근거·제외한 기법)

    @property
    def assumptions(self) -> list[Assumption]:
        return self.calculated.assumptions

    @property
    def premises(self) -> list[Driver]:
        return self.check.premises

    @property
    def excluded_assumptions(self) -> list[ExcludedAssumption]:
        return [*self.calculated.excluded_assumptions, *self.dropped]

    @property
    def excluded_drivers(self) -> list[ExcludedDriver]:
        return [*self.check.excluded_drivers, *self.calculated.excluded_drivers, *self.dropped_drivers]

    @property
    def judgments(self) -> list[StructuredJudgment]:
        return [*self.defined.judgments, *self.collection.judgments, *self.check.judgments, *self.calculated.judgments]


def _assumption_sources(assumptions: list[Assumption], premises: list[Driver]) -> dict[str, frozenset[SourceKey]]:
    """가정마다 보강에서 가장 먼저 쓸 소스: 그 가정의 원인과 전제가 근거로 삼는 소스(기본 데이터 `orders`는 제외)."""
    return {
        a.assumption_id: frozenset(k for k in required_evidence_for([a], premises) if k != ("orders", "same_item"))
        for a in assumptions
    }


def _uses_source(
    assumption: Assumption, premises: list[Driver], key: SourceKey, collection: DataCollectionResult
) -> bool:
    """가정이 소스를 쓰는가. `orders`는 모든 가정의 기본 데이터다. 그 밖의 소스는 가정의 원인과 전제가 근거로 삼거나
    그 가정의 이력 보강에 값을 낸 경우에 쓴다."""
    if key == ("orders", "same_item"):
        return True
    own = _assumption_sources([assumption], premises)[assumption.assumption_id]
    return key in own or key in collection.backcast_sources.get(assumption.assumption_id, [])


def _feed(digest, value: pd.Series | pd.DataFrame | None) -> None:
    if value is None:
        digest.update(b"none")
        return
    digest.update(np.asarray(value.index.values).tobytes())
    digest.update(np.ascontiguousarray(value.to_numpy(dtype=float)).tobytes())


def _fingerprint(assumption: Assumption, collection: DataCollectionResult, excluded_methods: set[str]) -> str:
    """가정 하나의 입력을 한 값으로 줄인다. 같으면 재실행이 같은 결과를 낸다."""
    digest = hashlib.sha1()
    digest.update(repr([(d.driver, d.evidence.kind, d.evidence.item_scope, tuple(d.evidence.refs)) for d in assumption.drivers]).encode())
    _feed(digest, collection.training_for(assumption.assumption_id))
    for key in required_evidence_for([assumption], []):
        digest.update(repr(key).encode())
        _feed(digest, collection.evidence_series.get(key))
    digest.update(repr(sorted(excluded_methods)).encode())
    return digest.hexdigest()


def _fingerprints(
    assumptions: list[Assumption], collection: DataCollectionResult, exclusions: dict[str, set[str]]
) -> dict[str, str]:
    return {a.assumption_id: _fingerprint(a, collection, exclusions.get(a.assumption_id, set())) for a in assumptions}


def run_forecast_steps(
    inputs: InstanceInputs, scheduled_promotion: bool = False, tracker: StageTracker | None = None
) -> ForecastStepsResult:
    """한 인스턴스의 내부 단계 1-4를 처음부터 실행한다."""
    stage = tracker or StageTracker()
    stage.step = "define_assumptions"
    definition = define_assumptions(inputs, scheduled_promotion)
    stage.step = "collect_data"
    collection = collect_instance_data(
        inputs, definition.required_evidence, None, _assumption_sources(definition.assumptions, definition.premises)
    )
    planning = last_complete_month(inputs.data_end)
    stage.step = "check_drivers"
    check = check_drivers(definition.assumptions, definition.premises, inputs, collection, planning)
    stage.step = "calculate_values"
    calculated = select_methods_and_calculate_values(check.assumptions, check.premises, inputs, collection, planning)
    fingerprints = _fingerprints(check.assumptions, collection, {})
    return ForecastStepsResult(
        scheduled_promotion, definition, collection, check, calculated, fingerprints=fingerprints
    )


def apply_steps_to_record(record: ForecastRecord, result: ForecastStepsResult) -> ForecastRecord:
    """내부 단계 결과 중 가정 선택 이전에 정해지는 필드를 반영한 새 레코드를 반환한다.

    `assumptions`, `scenario`, `selection_basis`는 가정 선택 단계가 쓴다.
    """
    return record.model_copy(
        update={
            "premises": result.premises,
            "excluded_drivers": result.excluded_drivers,
            "excluded_assumptions": result.excluded_assumptions,
            "data_sources": result.collection.data_sources,
            "excluded_sources": result.collection.excluded_sources,
            "cleaning": result.collection.cleaning,
        }
    )


class ForecastRerunHandling:
    """한 번의 재실행 처리. 기법 제외의 누적은 이 객체 안에만 있다(State 필드 없음)."""

    def __init__(self, inputs: InstanceInputs, result: ForecastStepsResult) -> None:
        self.inputs = inputs
        self.result = result
        self.tracker = StageTracker()
        self._method_exclusions: dict[str, set[str]] = {}

    def rerun(self, causes: SuspectedCause | list[SuspectedCause]) -> ForecastStepsResult:
        """의심되는 원인(들)로 재개 지점부터 이후 단계를 전부 다시 돌고, 결과를 다음 재실행의 직전 결과로 둔다."""
        cause_list = [causes] if isinstance(causes, SuspectedCause) else list(causes)
        if not cause_list:
            raise ValueError("의심되는 원인이 하나 이상 필요함")
        ordered = sorted(cause_list, key=lambda c: _RESUME_POINT[c.type])  # 단계 순서로 적용(같은 단계는 받은 순서)
        previous = self.result
        inputs = self.inputs
        reasons = [suspected_cause_reason(c) for c in ordered]
        stage = self.tracker
        stage.step = "apply_send_back"
        planning = last_complete_month(inputs.data_end)
        adjustments = previous.adjustments.copy()
        defined = previous.defined
        dropped = list(previous.dropped)
        dropped_drivers = list(previous.dropped_drivers)

        def dropped_ids() -> set[str]:
            return {d.assumption_id for d in dropped}

        def live_ids() -> list[str]:
            return [a.assumption_id for a in defined.assumptions if a.assumption_id not in dropped_ids()]

        def targets_of(cause: SuspectedCause) -> list[str]:
            """이 이유의 대상 가정. 입력 불변 규칙은 대상 가정에만 적용한다."""
            if cause.type == "assumption":
                return [cause.assumption_id] if cause.assumption_id is not None else []
            if cause.type == "method_selection" or cause.source is None:
                return live_ids()
            key = (cause.source.kind, cause.source.item_scope)
            return [
                a.assumption_id
                for a in defined.assumptions
                if a.assumption_id in live_ids() and _uses_source(a, defined.premises, key, previous.collection)
            ]

        target_ids = [targets_of(c) for c in ordered]  # 직전 결과 기준으로 먼저 정한다
        all_targets = {i for ids_ in target_ids for i in ids_}

        def reasons_for(assumption_id: str) -> list[str]:
            """가정을 제외하는 이유: 그 가정을 대상으로 한 모든 이유(단계 순서, 같은 단계는 받은 순서)."""
            own = [r for r, ids_ in zip(reasons, target_ids) if assumption_id in ids_] or reasons
            return list(dict.fromkeys(own))

        def drop(ids: list[str], rationale: str) -> None:
            for assumption_id in ids:
                if assumption_id not in dropped_ids():
                    dropped.append(
                        ExcludedAssumption(assumption_id=assumption_id, reasons=reasons_for(assumption_id), rationale=rationale)
                    )

        # 1단계: assumption 이유 — 한 가정의 이유를 한꺼번에 반영한다
        assumption_causes = [c for c in ordered if c.type == "assumption"]
        if assumption_causes:
            stage.step = "redefine_assumptions"
            redefinition = redefine_assumptions(defined, assumption_causes)
            defined = redefinition.definition
            dropped += [e for e in redefinition.excluded_assumptions if e.assumption_id not in dropped_ids()]
            dropped_drivers += redefinition.excluded_drivers

        # 2단계: data_source 이유
        for cause, ids_ in zip(ordered, target_ids):
            if cause.type != "data_source":
                continue
            key: SourceKey | None = (cause.source.kind, cause.source.item_scope) if cause.source is not None else None
            if cause.issue == "insufficient":
                adjustments.force_supplement |= set(ids_)
            elif not ids_:
                pass  # 이 소스를 쓰는 가정이 없으면 어떤 대응도 하지 않는다
            elif cause.issue == "outdated" and cause.use_from is not None and key is not None:
                adjustments.use_from[key] = cause.use_from
            elif key is not None and key != ("orders", "same_item"):
                adjustments.exclude[key] = "contaminated" if cause.issue == "contaminated" else "irrelevant"
            # orders가 오염이면 소스를 제외할 수 없다. 입력이 바뀌지 않으므로 아래 규칙이 모든 가정을 제외한다

        # 3단계: method_selection 이유
        for cause, ids_ in zip(ordered, target_ids):
            if cause.type != "method_selection":
                continue
            for assumption in previous.assumptions:
                if assumption.assumption_id in ids_:
                    used = {m.method for m in assumption.method_values}
                    self._method_exclusions.setdefault(assumption.assumption_id, set()).update(used)

        start = min(_RESUME_POINT[c.type] for c in ordered)
        if start <= 2:
            live = [a for a in defined.assumptions if a.assumption_id not in dropped_ids()]
            stage.step = "collect_data"
            collection = collect_instance_data(
                inputs, required_evidence_for(live, defined.premises), adjustments,
                _assumption_sources(live, defined.premises),
            )
            if collection.unusable_reason is not None:
                drop(live_ids(), f"의심되는 원인을 반영하면 사용할 주문이 없음: {collection.unusable_reason}")
            live = [a for a in defined.assumptions if a.assumption_id not in dropped_ids()]
            stage.step = "check_drivers"
            check = check_drivers(live, defined.premises, inputs, collection, planning)
        else:
            collection = previous.collection
            check = DriverCheck(
                assumptions=[a for a in previous.check.assumptions if a.assumption_id not in dropped_ids()],
                premises=previous.check.premises,
                excluded_drivers=previous.check.excluded_drivers,
                judgments=previous.check.judgments,
            )

        # 모든 이유를 적용한 뒤 대상 가정마다 한 번만 비교한다. 입력이 직전 재실행과 같으면 수단이 없다
        fingerprints = _fingerprints(check.assumptions, collection, self._method_exclusions)
        unchanged = [
            a.assumption_id
            for a in check.assumptions
            if a.assumption_id in all_targets and previous.fingerprints.get(a.assumption_id) == fingerprints[a.assumption_id]
        ]
        drop(unchanged, "의심되는 원인을 반영해도 입력이 직전 재실행과 같아 수단이 없음")
        check = DriverCheck(
            assumptions=[a for a in check.assumptions if a.assumption_id not in dropped_ids()],
            premises=check.premises,
            excluded_drivers=check.excluded_drivers,
            judgments=check.judgments,
        )

        stage.step = "calculate_values"
        calculated = select_methods_and_calculate_values(
            check.assumptions, check.premises, inputs, collection, planning, self._method_exclusions
        )
        if any(c.type == "method_selection" for c in ordered):  # 기법 제외 뒤 남은 구성이 없는 가정은 의심되는 원인으로 제외한다
            kept: list[ExcludedAssumption] = []
            for excluded in calculated.excluded_assumptions:
                if excluded.assumption_id in self._method_exclusions:
                    dropped.append(
                        ExcludedAssumption(
                            assumption_id=excluded.assumption_id,
                            reasons=reasons_for(excluded.assumption_id),
                            rationale=f"직전 기법 구성을 제외한 뒤 남은 기법 구성이 없음: {excluded.rationale}",
                        )
                    )
                else:
                    kept.append(excluded)
            calculated.excluded_assumptions = kept

        log(ROLE_TAG, "rerun", causes=reasons, resume_step=start,
            assumptions=[a.assumption_id for a in calculated.assumptions], dropped=[d.assumption_id for d in dropped])
        self.result = ForecastStepsResult(
            previous.scheduled_promotion, defined, collection, check, calculated, adjustments, dropped,
            dropped_drivers, fingerprints,
        )
        return self.result
