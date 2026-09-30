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
ScenarioDriver = Literal["category_trend", "price", "event"]
ScenarioDefinedBy = Literal["rule", "agent_judgment"]

# 되돌림 사유 — type은 문제가 난 대상, issue는 그 대상의 하위 사유
CauseType = Literal["scenario", "data_source", "forecast_method"]
ScenarioIssue = Literal["no_evidence", "effect_out_of_range", "not_distinct", "double_counted"]
DataSourceIssue = Literal["insufficient", "contaminated", "irrelevant"]
CauseIssue = ScenarioIssue | DataSourceIssue

_SCENARIO_ISSUES = frozenset(get_args(ScenarioIssue))
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
    scenario_id: str | None = None  # type이 scenario일 때 문제가 난 시나리오
    source: SourceRef | None = None  # type이 data_source일 때 문제가 난 소스
    use_from: date | None = None  # 시점 기준으로 무관할 때

    @model_validator(mode="after")
    def _check_type_issue_consistency(self) -> "SuspectedCause":
        if self.type == "scenario":
            if self.issue not in _SCENARIO_ISSUES:
                raise ValueError(f"type=scenario의 issue는 {sorted(_SCENARIO_ISSUES)} 중 하나여야 함")
        elif self.type == "data_source":
            if self.issue not in _DATA_SOURCE_ISSUES:
                raise ValueError(
                    f"type=data_source의 issue는 {sorted(_DATA_SOURCE_ISSUES)} 중 하나여야 함"
                )
        elif self.issue is not None:
            raise ValueError("type=forecast_method는 issue가 없어야 함")
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
    """가정의 근거 데이터. data_sources와 같은 어휘(kind, item_scope)를 쓴다."""

    kind: DataKind
    item_scope: ItemScope
    refs: list[str] = Field(default_factory=list)


class Assumption(BaseModel):
    driver: ScenarioDriver
    demand_effect: float  # 수요 변화율(부호 있음, +0.1 = 10% 증가)
    evidence: Evidence


class Scenario(BaseModel):
    """서로 다른 가정에서 나온 서로 다른 예측. 가정이 여러 개면 동시에 일어나는
    하나의 미래이며, 효과를 합쳐 예측값 하나·비용 하나를 갖는다.

    value/forecast_uncertainty/likelihood/cost_estimate는 시나리오 정의(1단계)
    직후에는 아직 계산 전이라 없을 수 있다(계산은 후속 내부 단계가 채운다).
    """

    scenario_id: str
    assumptions: list[Assumption] = Field(default_factory=list)  # 비어 있으면 기준 시나리오
    defined_by: ScenarioDefinedBy = "rule"
    value: float | None = None
    forecast_uncertainty: float | None = None
    likelihood: float | None = None
    cost_estimate: float | None = None


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
    scenarios: list[Scenario] = Field(default_factory=list)
    data_sources: list[DataSource] = Field(default_factory=list)
    excluded_sources: list[ExcludedSource] = Field(default_factory=list)
    cleaning: Cleaning = Field(default_factory=Cleaning)
    forecast_method: str | None = None
    selected_scenario: str | None = None  # 선택된 scenario_id
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
    """forecast->human_manager 알림 기준 — 예측과 약정 잔여량의 차이 비율.

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
