"""State의 7개 최상위 필드 정의. 구조는 STATE_SCHEMA.md를 따른다.

State 스키마 자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일과
STATE_SCHEMA.md를 함께 고친다 — 바뀐 이유는 JOURNAL.md에 남긴다.
"""

from datetime import date
from typing import Literal, get_args

from pydantic import BaseModel, Field, model_validator

ValidationStatus = Literal["passed", "flagged", "check_failed"]
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
# 수요 동인 — 데이터가 실제로 존재하는 요인만 값으로 둔다(자유 텍스트 금지)
DriverName = Literal["category_trend", "price", "event"]
AssumptionDefinedBy = Literal["rule", "agent_judgment"]

# 되돌림 사유 — type은 문제가 난 대상, issue는 그 대상의 하위 사유
CauseType = Literal["assumption", "data_source", "method_selection"]
AssumptionIssue = Literal["no_evidence", "value_out_of_range", "not_distinct", "double_counted"]
DataSourceIssue = Literal["insufficient", "contaminated", "irrelevant"]
CauseIssue = AssumptionIssue | DataSourceIssue

_ASSUMPTION_ISSUES = frozenset(get_args(AssumptionIssue))
_DATA_SOURCE_ISSUES = frozenset(get_args(DataSourceIssue))


class SourceRef(BaseModel):
    """suspected_cause가 가리키는 데이터 소스 (kind, item_scope)."""

    kind: DataKind
    item_scope: ItemScope


class SuspectedCause(BaseModel):
    """검증agent의 flagged와 supply_coordination→forecast 역방향 되돌림이 같은
    형식으로 보내는 되돌림 사유. type별로 유효한 issue가 정해져 있다(STATE_SCHEMA.md).
    """

    type: CauseType
    issue: CauseIssue | None = None
    assumption_id: str | None = None  # type이 assumption일 때 문제가 난 가정
    source: SourceRef | None = None  # type이 data_source일 때 문제가 난 소스
    use_from: date | None = None  # 시점 기준으로 무관할 때

    @model_validator(mode="after")
    def _check_type_issue_consistency(self) -> "SuspectedCause":
        if self.type == "assumption":
            if self.issue not in _ASSUMPTION_ISSUES:
                raise ValueError(f"type=assumption의 issue는 {sorted(_ASSUMPTION_ISSUES)} 중 하나여야 함")
        elif self.type == "data_source":
            if self.issue not in _DATA_SOURCE_ISSUES:
                raise ValueError(
                    f"type=data_source의 issue는 {sorted(_DATA_SOURCE_ISSUES)} 중 하나여야 함"
                )
        elif self.issue is not None:
            raise ValueError("type=method_selection은 issue가 없어야 함")
        return self


class ValidationResult(BaseModel):
    """forecast_records/exchanges 등에 내장되는 현재값 전용 검증 상태.

    이력은 여기가 아니라 negotiation_log에 쌓인다(판단용 현재값과 기록용
    스냅샷 분리 — STATE_SCHEMA.md).
    """

    status: ValidationStatus
    suspected_cause: SuspectedCause | None = None
    rationale: str | None = None
    ts: str | None = None
    validator_role_tag: str | None = None


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
    """관련 없는 데이터 — 재실행 시 다시 고르지 않는다."""

    kind: DataKind
    item_scope: ItemScope
    refs: list[str] = Field(default_factory=list)
    reason: Literal["irrelevant"] = "irrelevant"


class Cleaning(BaseModel):
    """표준 정제 적용 기록."""

    applied: bool = False
    count: int = 0


class ExcludedDriver(BaseModel):
    """근거가 부족하거나 효과가 유의하지 않아 뺀 요인의 기록과, 영향받은 가정.

    영향받은 가정은 그 요인을 단 가정(제외됨) 또는 모든 가정(전제인 요인이 빠짐)이다. `reason`은
    `no_significant_effect`(통계 추정에서 신뢰구간이 0을 포함), `no_evidence`(근거 데이터
    없음·부족), `no_applicable_method`(전제를 설명변수로 받는 기법이 계산되지 않아 반영하지 못함)로 구분한다.
    """

    driver: DriverName
    assumption_ids: list[str]
    reason: Literal["no_significant_effect", "no_evidence", "no_applicable_method"]
    rationale: str


class ExcludedAssumption(BaseModel):
    """맞는 통계기법이 하나도 없어 제외한 가정과 이유."""

    assumption_id: str
    reason: Literal["no_applicable_method"]
    rationale: str


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


# --- 4. negotiation_log --------------------------------------------------------


class NegotiationLogEntry(BaseModel):
    role_tag: str
    event: str
    round: int | None = None
    ts: str


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
    """사람(human_manager)에게 올라간 건. intervention은 진행을 멈추고 결정을
    기다리며, notice는 알리기만 하고 진행한다(resolution 없음)."""

    trigger_edge: str
    reason: str
    target_role: Literal["human_manager"] = "human_manager"
    mode: EscalationMode = "intervention"
    status: str
    resolution: str | None = None  # intervention일 때만


class State(BaseModel):
    forecast_records: list[ForecastRecord] = Field(default_factory=list)
    capacity_pools: list[CapacityPool] = Field(default_factory=list)
    allocation_candidates: list[AllocationCandidate] = Field(default_factory=list)
    negotiation_log: list[NegotiationLogEntry] = Field(default_factory=list)
    interaction_protocol: list[InteractionProtocol] = Field(default_factory=list)
    role_permissions: list[RolePermission] = Field(default_factory=list)
    escalation_records: list[EscalationRecord] = Field(default_factory=list)
