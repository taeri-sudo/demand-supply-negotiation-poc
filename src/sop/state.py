"""State의 8개 최상위 필드 정의. 구조는 STATE_SCHEMA.md를 따른다.

State 스키마 자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일과
STATE_SCHEMA.md를 함께 고친다 — 바뀐 이유는 JOURNAL.md에 남긴다.
"""

from datetime import date
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

ValidationStatus = Literal["passed", "failed", "error"]
SelectionBasis = Literal["rule", "agent_judgment", "human"]
CandidateStatus = Literal["generated", "selected", "rejected"]
ResponseStatus = Literal["feasible", "infeasible", "in_progress"]
EscalationKind = Literal["rule", "agent_judgment", "human"]
EscalationMode = Literal["intervention", "notice"]
ProtocolSource = Literal["initial_design", "promoted_from_trace", "external_benchmark"]
AccessMode = Literal["r", "w"]

# 데이터 소스 — kind(출처)와 item_scope(예측 대상 item과의 관계)는 독립된 두 변수
DataKind = Literal["orders", "pos", "market"]
ItemScope = Literal["same_item", "similar_item", "category"]
# 수요 동인 — 데이터가 실제로 존재하는 원인만 값으로 둔다(자유 텍스트 금지)
DriverName = Literal["category_trend", "price", "event"]
AssumptionDefinedBy = Literal["rule", "agent_judgment"]

# 의심되는 원인 — type은 문제 대상, issue는 문제 내용
CauseType = Literal["assumption", "data_source", "method_selection", "misrouted"]
AssumptionIssue = Literal["no_evidence", "value_out_of_range", "not_distinct", "double_counted"]
DataSourceIssue = Literal["insufficient", "contaminated", "irrelevant", "outdated"]
CauseIssue = AssumptionIssue | DataSourceIssue

_ASSUMPTION_ISSUES = frozenset(get_args(AssumptionIssue))
_DATA_SOURCE_ISSUES = frozenset(get_args(DataSourceIssue))
# 문제 원인을 지목할 수 있는 문제 내용: 근거가 없는 원인들, 근거가 겹치는 원인들. 나머지는 항상 null
_DRIVER_POINTING_ISSUES = frozenset(["no_evidence", "double_counted"])


class SourceRef(BaseModel):
    """suspected_cause가 가리키는 데이터 소스 (kind, item_scope)."""

    kind: DataKind
    item_scope: ItemScope


class SuspectedCause(BaseModel):
    """불합격 판정(`validation`)과 send-back(`send_back`)이 같은
    형식으로 싣는 의심되는 원인 하나(한 번에 목록으로 보낸다). 문제 대상(type)별로 유효한 문제 내용(issue)과
    대상 필드가 정해져 있다(STATE_SCHEMA.md).
    """

    type: CauseType
    issue: CauseIssue | None = None
    assumption_id: str | None = None  # type이 assumption일 때 문제가 난 가정
    drivers: list[DriverName] | None = None  # type이 assumption일 때, 문제 원인을 지목할 수 있으면 그 원인들(null = 그 가정의 모든 원인)
    source: SourceRef | None = None  # type이 data_source일 때 문제가 난 소스
    use_from: date | None = None  # outdated일 때, 이 날짜 이전 데이터는 쓰지 않음

    @model_validator(mode="after")
    def _check_type_issue_consistency(self) -> "SuspectedCause":
        if self.type == "assumption":
            if self.issue not in _ASSUMPTION_ISSUES:
                raise ValueError(f"type=assumption의 issue는 {sorted(_ASSUMPTION_ISSUES)} 중 하나여야 함")
            if self.assumption_id is None:
                raise ValueError("type=assumption에는 assumption_id가 필요함")
            if self.drivers is not None:
                if self.issue not in _DRIVER_POINTING_ISSUES:
                    raise ValueError(f"drivers는 issue가 {sorted(_DRIVER_POINTING_ISSUES)}일 때만 쓴다(그 밖에는 원인을 지목할 수 없어 null)")
                if not self.drivers or len(set(self.drivers)) != len(self.drivers):
                    raise ValueError("drivers는 비어 있지 않고 중복이 없어야 함(모든 원인은 null로 나타낸다)")
        elif self.type == "data_source":
            if self.issue not in _DATA_SOURCE_ISSUES:
                raise ValueError(
                    f"type=data_source의 issue는 {sorted(_DATA_SOURCE_ISSUES)} 중 하나여야 함"
                )
            if self.assumption_id is not None or self.drivers is not None:
                raise ValueError("type=data_source에는 assumption_id·drivers가 없음")
            if self.issue in ("contaminated", "irrelevant", "outdated") and self.source is None:
                raise ValueError(f"issue={self.issue}에는 source가 필요함")
            if self.issue == "outdated" and self.use_from is None:
                raise ValueError("issue=outdated에는 use_from이 필요함")
            if self.use_from is not None and self.issue != "outdated":
                raise ValueError("use_from은 issue=outdated일 때만 쓴다")
            if (
                self.issue == "irrelevant"
                and self.source is not None
                and (self.source.kind, self.source.item_scope) == ("orders", "same_item")
            ):
                raise ValueError("orders의 irrelevant는 허용되지 않음(orders는 항상 쓰므로 소스를 제외할 수 없고 outdated만 받음)")
        else:
            if self.issue is not None:
                raise ValueError(f"type={self.type}은 issue가 없어야 함")
            if (
                self.assumption_id is not None or self.drivers is not None
                or self.source is not None or self.use_from is not None
            ):
                raise ValueError(f"type={self.type}에는 assumption_id·drivers·source·use_from이 없음(method_selection의 대상은 모든 가정)")
        return self


class ValidationResult(BaseModel):
    """forecast_records/exchanges 등에 내장되는 현재값 전용 검증 상태.

    이력은 여기가 아니라 role_logs에 쌓인다(판단용 현재값과 기록용
    스냅샷 분리 — STATE_SCHEMA.md).
    """

    status: ValidationStatus
    suspected_causes: list[SuspectedCause] = Field(default_factory=list)  # failed일 때 1개 이상, 그 밖에는 빈 목록
    rationale: str | None = None
    ts: str | None = None
    validator_role_tag: str | None = None

    @model_validator(mode="after")
    def _check_causes_follow_status(self) -> "ValidationResult":
        if self.status == "failed" and not self.suspected_causes:
            raise ValueError("status=failed에는 suspected_causes가 1개 이상 필요함")
        if self.status != "failed" and self.suspected_causes:
            raise ValueError("suspected_causes는 status=failed일 때만 쓴다")
        if any(cause.type == "misrouted" for cause in self.suspected_causes):
            raise ValueError("misrouted는 send_back에만 쓴다(검증agent는 경로 밖이라 되돌리지 않는다)")
        return self


class SendBack(BaseModel):
    """기록을 쓴 agent(작성agent)에게 되돌리는 쪽이 기록에 쓰는 send-back. `suspected_causes`는 `ValidationResult`의 것과 같은 형식이다."""

    from_role: str
    suspected_causes: list[SuspectedCause] = Field(min_length=1)
    ts: str

    @model_validator(mode="after")
    def _misrouted_is_sent_alone(self) -> "SendBack":
        if any(c.type == "misrouted" for c in self.suspected_causes) and len(self.suspected_causes) > 1:
            raise ValueError("misrouted는 다른 이유와 함께 보내지 않는다")
        return self

    @property
    def is_misrouted(self) -> bool:
        return self.suspected_causes[0].type == "misrouted"


# --- 1. forecast_records ------------------------------------------------------


class Evidence(BaseModel):
    """driver의 근거 데이터. data_sources와 같은 어휘(kind, item_scope)를 쓴다."""

    kind: DataKind
    item_scope: ItemScope
    refs: list[str] = Field(default_factory=list)


class Driver(BaseModel):
    """요청량을 바꾸는 원인(수요 동인) 하나와 그 근거. 가정의 `drivers[]` 항목이다."""

    driver: DriverName
    evidence: Evidence


class MethodValue(BaseModel):
    """통계기법 하나가 계산한 요청량(기법별 값)과 과거 정확도로 구한 기법 가중치."""

    method: str
    value: float
    method_weight: float


class Assumption(BaseModel):
    """선택지 1개. driver 몇 개와 통계기법 여러 개를 엮어 계산한 하나의 경우의 수다.

    `drivers`가 비어 있으면 기본 가정이며, 기법별 값은 우리 주문 이력만으로 계산한 값이다.
    `value`는 기법별 값을 합치거나 하나 선택해서 정한 가정의 값이다. 가정 정의 직후에는
    기법별 값과 value가 아직 없다(계산은 후속 내부 단계가 채운다).
    """

    assumption_id: str
    drivers: list[Driver] = Field(default_factory=list)
    defined_by: AssumptionDefinedBy = "rule"
    method_values: list[MethodValue] = Field(default_factory=list)
    value: float | None = None
    occurrence_likelihood: float | None = None  # 가정 발생 가능성 — 근거가 있을 때만
    forecast_uncertainty: float | None = None


ScenarioDerivation = Literal["pass_through", "chosen", "mean", "median"]


class Scenario(BaseModel):
    """가정들의 value 중에서 택하거나 계산해서 정한 최종 요청량과, 어느 가정에서 왔는지."""

    value: float
    assumption_ids: list[str]
    derivation: ScenarioDerivation


class DataSource(BaseModel):
    kind: DataKind
    item_scope: ItemScope
    refs: list[str] = Field(default_factory=list)
    use_from: date | None = None  # 이 날짜 이전 데이터는 쓰지 않음


class ExcludedSource(BaseModel):
    """쓰지 않는 데이터 — 재실행 시 다시 고르지 않는다. `irrelevant`는 관련 없는 데이터, `contaminated`는
    의심되는 원인이 오염이라 제외한 데이터다."""

    kind: DataKind
    item_scope: ItemScope
    refs: list[str] = Field(default_factory=list)
    reason: Literal["irrelevant", "contaminated"] = "irrelevant"


class Cleaning(BaseModel):
    """표준 정제 적용 기록."""

    applied: bool = False
    count: int = 0


class ExcludedDriver(BaseModel):
    """근거가 부족하거나 효과가 유의하지 않아 제외한 원인의 기록과, 영향받은 가정.

    영향받은 가정은 그 원인을 단 가정(제외됨) 또는 모든 가정(전제인 원인이 제외됨)이다. `reasons`의 값은
    `no_significant_effect`(통계 추정에서 신뢰구간이 0을 포함), `no_evidence`(근거 데이터
    없음·부족), `no_applicable_method`(전제를 설명변수로 받는 기법이 계산되지 않아 반영하지 못함) 중 하나이거나,
    그 원인을 제외하게 한 의심되는 원인(`suspected_cause_reason`)다. 이유가 여럿이면 모두 넣는다.
    """

    driver: DriverName
    assumption_ids: list[str]
    reasons: list[str]
    rationale: str

    @model_validator(mode="after")
    def _check_reasons(self) -> "ExcludedDriver":
        if not self.reasons:
            raise ValueError("reasons는 비어 있으면 안 됨")
        for reason in self.reasons:
            if reason not in _DRIVER_REASONS and reason not in _SUSPECTED_CAUSE_REASONS:
                raise ValueError(f"reasons의 값은 {sorted(_DRIVER_REASONS)} 또는 의심되는 원인여야 함: {reason!r}")
        return self


_DRIVER_REASONS = frozenset(["no_significant_effect", "no_evidence", "no_applicable_method"])


def suspected_cause_reason(cause: SuspectedCause) -> str:
    """의심되는 원인을 제외 이유 목록(`reasons`)에 넣는 문자열로 만든다: `{type}:{issue}`(issue가 없으면 `{type}`)."""
    return cause.type if cause.issue is None else f"{cause.type}:{cause.issue}"


_SUSPECTED_CAUSE_REASONS = frozenset(
    [*(f"assumption:{i}" for i in _ASSUMPTION_ISSUES), *(f"data_source:{i}" for i in _DATA_SOURCE_ISSUES), "method_selection"]
)


class ExcludedAssumption(BaseModel):
    """계산할 수 없어 제외한 가정과 이유. `reasons`의 값은 `no_applicable_method`(맞는 통계기법이 하나도 없음)이거나
    그 가정을 제외하게 한 의심되는 원인(`suspected_cause_reason`)이고, 이유가 여럿이면 모두 넣는다."""

    assumption_id: str
    reasons: list[str]
    rationale: str

    @model_validator(mode="after")
    def _check_reasons(self) -> "ExcludedAssumption":
        if not self.reasons:
            raise ValueError("reasons는 비어 있으면 안 됨")
        for reason in self.reasons:
            if reason != "no_applicable_method" and reason not in _SUSPECTED_CAUSE_REASONS:
                raise ValueError(f"reasons의 값은 no_applicable_method 또는 의심되는 원인여야 함: {reason!r}")
        return self


class ForecastRecord(BaseModel):
    """forecast agent 인스턴스((회사, item) 조합별 1개)가 남기는 현재값 기록.

    agent_id는 "{company_id}:{item_id}" 같은 표시용 합성키이고, 조회는
    company_id/item_id 필드로 한다. company_id는 필수다 — 고객사가 정해지지
    않은 수량은 forecast를 거치지 않는 human_input 수요다(M4).
    """

    agent_id: str
    company_id: str
    item_id: str
    pool_key: str | None = None
    role_tag: Literal["forecast"] = "forecast"
    assumptions: list[Assumption] = Field(default_factory=list)
    premises: list[Driver] = Field(default_factory=list)  # 모든 가정의 전제(확정된 프로모션 일정)
    excluded_drivers: list[ExcludedDriver] = Field(default_factory=list)
    excluded_assumptions: list[ExcludedAssumption] = Field(default_factory=list)
    data_sources: list[DataSource] = Field(default_factory=list)
    excluded_sources: list[ExcludedSource] = Field(default_factory=list)
    cleaning: Cleaning = Field(default_factory=Cleaning)
    scenario: Scenario | None = None
    selection_basis: SelectionBasis | None = None
    validation: ValidationResult | None = None
    forward_to: str | None = None  # passed를 확인한 작성agent가 정한 다음 agent의 role_tag
    send_back: SendBack | None = None  # 작성agent에게 되돌리는 쪽이 쓴다


# --- 2. capacity_pools --------------------------------------------------------


class CapacityAdjustment(BaseModel):
    ts: str
    delta: float
    reason: str


class CapacityPool(BaseModel):
    pool_id: str
    total_capacity: float
    remaining_capacity: float
    adjustment_history: list[CapacityAdjustment] = Field(default_factory=list)
    linked_pool_key: str | None = None


# --- 3. allocation_candidates -------------------------------------------------


class Exchange(BaseModel):
    role_tag: str
    round: int
    request: dict
    response: dict | None = None
    response_status: ResponseStatus | None = None
    requested_at: str | None = None
    responded_at: str | None = None
    handled_by: str | None = None
    routing_reason: str | None = None
    validation: ValidationResult | None = None


class AllocationCandidate(BaseModel):
    plan_id: str
    allocation: dict[str, float] = Field(default_factory=dict)
    cost: float | None = None
    risk: float | None = None
    status: CandidateStatus = "generated"
    required_stages: list[str] = Field(default_factory=list)
    exchanges: list[Exchange] = Field(default_factory=list)


# --- 4. role_logs ---------------------------------------------------------------

# 사건 이름은 정해진 것만 쓴다. 값(agent_id, plan_id, 의심되는 원인, 판정 시각 등)은 이름에 넣지 않고 필드와 payload에 담는다
LogEvent = Literal[
    "scenario_decided",
    "scenario_not_computable",
    "scenario_options_exhausted",
    "rerun",
    "forwarded",
    "next_agent_unresolved",
    "misrouted_send_back",
    "forecast_run_error",
    "forecast_run_skipped",
    "allocation_candidate_generated",
    "validation_passed",
    "validation_failed",
    "validation_error",
]


class LogEntry(BaseModel):
    """역할별 로그의 항목. `seq`는 모든 역할을 통틀어 하나로 늘어나는 순번이고 로그 쓰기 함수가 붙인다."""

    seq: int
    ts: str
    role_tag: str
    agent_id: str | None = None
    event: LogEvent
    round: int | None = None
    payload: dict = Field(default_factory=dict)


def merge_role_logs(role_logs: dict[str, list[LogEntry]]) -> list[LogEntry]:
    """`negotiation_log`: 역할별 로그를 `seq` 순으로 합쳐 읽은 결과. 저장하지 않는다."""
    return sorted((entry for entries in role_logs.values() for entry in entries), key=lambda entry: entry.seq)


# --- 5. interaction_protocol ---------------------------------------------------


class NoticeThreshold(BaseModel):
    """supply_coordination->human_manager 알림 기준 — 약정 잔여량과의 차이 비율(처리 규칙은 M4·M5에서 정함).

    구속력이 없으면 우리가 손실을 떠안으므로 더 작은 차이에도 알린다.
    """

    binding: float
    non_binding: float


class InteractionProtocol(BaseModel):
    edge: str
    max_rounds: int | None = None
    repeat_escalation_threshold: int | None = None
    scope: list[str]
    escalation_trigger: str | None = None
    escalation_target: str | None = None
    escalation_kind: EscalationKind | None = None
    escalation_mode: EscalationMode | None = None
    notice_threshold: NoticeThreshold | None = None
    source: ProtocolSource = "initial_design"
    last_updated: str | None = None


# --- 6. role_permissions ---------------------------------------------------------


class RolePermission(BaseModel):
    role_tag: str
    field_path: str
    access: AccessMode


# --- 7. escalation_records -----------------------------------------------------


class EscalationRecord(BaseModel):
    """사람(human_manager)에게 올라간 escalation 기록. intervention은 진행을 멈추고 결정을
    기다리며, notice는 알리기만 하고 진행한다(resolution 없음)."""

    agent_id: str | None = None  # escalation 기록이 발생한 인스턴스("{company_id}:{item_id}"). 인스턴스와 무관한 엣지는 None
    trigger_edge: str
    reason: str
    rationale: str  # 사람이 읽는 이유 설명
    target_role: Literal["human_manager"] = "human_manager"
    mode: EscalationMode = "intervention"
    log_seq: int | None = None  # 이 escalation과 관련된 로그 항목의 seq(role_logs)
    status: str
    resolution: str | None = None  # intervention일 때만


# --- 8. agent_cards -------------------------------------------------------------

ResponseType = Literal["optimization", "handoff", "round_accumulation"]


class AgentCard(BaseModel):
    """경로 위 agent의 카드(STATE_SCHEMA.md 8번 절). 다음 agent 선택과 받는 쪽 반송의 기준이다."""

    role_tag: str
    description: str  # 하는 일
    accepts: str | None = None  # 받는 것
    produces: str | None = None  # 내는 것
    response_type: ResponseType  # 넘겨받은 기록에 응답하는 방식
    known_agents: list[str] = Field(default_factory=list)  # 이 agent가 넘길 수 있는 agent의 role_tag


class State(BaseModel):
    forecast_records: list[ForecastRecord] = Field(default_factory=list)
    capacity_pools: list[CapacityPool] = Field(default_factory=list)
    allocation_candidates: list[AllocationCandidate] = Field(default_factory=list)
    role_logs: dict[str, list[LogEntry]] = Field(default_factory=dict)
    interaction_protocol: list[InteractionProtocol] = Field(default_factory=list)
    role_permissions: list[RolePermission] = Field(default_factory=list)
    escalation_records: list[EscalationRecord] = Field(default_factory=list)
    agent_cards: list[AgentCard] = Field(default_factory=list)
