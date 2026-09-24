"""State의 7개 최상위 필드 정의. 구조는 STATE_SCHEMA.md를 따른다.

State 스키마 자체가 바뀌면(필드 추가/제거/형태 변경) 이 파일과
STATE_SCHEMA.md를 함께 고친다 — 바뀐 이유는 JOURNAL.md에 남긴다.
"""

from pydantic import BaseModel, Field
from typing import Literal

ValidationStatus = Literal["passed", "flagged", "check_failed"]
DataSourceBasis = Literal["own_company", "similar_companies"]
SelectionBasis = Literal["rule", "agent_judgment", "human"]
CandidateStatus = Literal["generated", "selected", "rejected"]
ResponseStatus = Literal["feasible", "infeasible", "in_progress"]
EscalationKind = Literal["rule", "agent_judgment", "human"]
ProtocolSource = Literal["initial_design", "promoted_from_trace", "external_benchmark"]
AccessMode = Literal["r", "w"]


class ValidationResult(BaseModel):
    """forecast_agents/exchanges 등에 내장되는 현재값 전용 검증 상태.

    이력은 여기가 아니라 negotiation_log에 쌓인다(판단용 현재값과 기록용
    스냅샷 분리 — STATE_SCHEMA.md).
    """

    status: ValidationStatus
    suspected_cause: str | None = None
    rationale: str | None = None
    ts: str | None = None
    validator_role_tag: str | None = None


# --- 1. forecast_agents ------------------------------------------------------
# 원래 analysis_agents/forecast_agents 두 필드였으나 analysis agent와
# forecast agent를 하나로 통합하며 합쳤다(되돌림 지점이 "데이터 소스
# 문제"/"모델 선택 문제" 두 값으로 충분해져 agent 분리 이유가 사라짐 —
# JOURNAL.md 2026-09-20 참고). data_source_basis/model_selection/candidates는
# 실제 데이터 소스 판단·모델 선택 로직이 아직 없어(M2 공백) 값이 채워지지
# 않을 수 있어 선택 필드로 둔다.


class ForecastCandidate(BaseModel):
    scenario: str
    value: float
    confidence: float
    cost_estimate: float


class ForecastAgentRecord(BaseModel):
    agent_id: str
    company_id: str | None = None
    item_id: str
    pool_key: str | None = None
    role_tag: Literal["forecast"] = "forecast"
    data_source_basis: DataSourceBasis | None = None
    model_selection: str | None = None
    candidates: list[ForecastCandidate] = Field(default_factory=list)
    selected: str | None = None
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


class InteractionProtocol(BaseModel):
    edge: str
    max_rounds: int | None = None
    repeat_escalation_threshold: int | None = None
    scope: list[str]
    escalation_trigger: str | None = None
    escalation_target: str | None = None
    escalation_kind: EscalationKind | None = None
    source: ProtocolSource = "initial_design"
    last_updated: str | None = None


# --- 6. role_permissions ---------------------------------------------------------


class RolePermission(BaseModel):
    role_tag: str
    field_path: str
    access: AccessMode


# --- 7. escalation_records -----------------------------------------------------


class EscalationRecord(BaseModel):
    trigger_edge: str
    reason: str
    target_role: Literal["human_manager"] = "human_manager"
    status: str
    resolution: str | None = None


class State(BaseModel):
    forecast_agents: list[ForecastAgentRecord] = Field(default_factory=list)
    capacity_pools: list[CapacityPool] = Field(default_factory=list)
    allocation_candidates: list[AllocationCandidate] = Field(default_factory=list)
    negotiation_log: list[NegotiationLogEntry] = Field(default_factory=list)
    interaction_protocol: list[InteractionProtocol] = Field(default_factory=list)
    role_permissions: list[RolePermission] = Field(default_factory=list)
    escalation_records: list[EscalationRecord] = Field(default_factory=list)
